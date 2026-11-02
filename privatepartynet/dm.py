"""Direct messages between two PCs, with no party and no server in between.

Each install runs a small TLS listener (`/dm`). A message is sealed to the recipient's key with a fresh ephemeral key and
signed by the sender (identity.seal_to), so the connection's certificate is not relied on for anything: a PC that
presents the wrong certificate learns nothing, and one that is not the recipient cannot open the message. The envelope
that travels names the sender's public keys; the receiver checks that the id is the hash of those keys, then pins them
on first sight (a later message from the same id with different keys is refused).

A message to someone who is not reachable waits in the encrypted outbox and goes out when they are seen again, with
growing pauses between tries. A message is saved on the sender's PC the moment it is written, and on the receiver's PC
when it arrives; the receiver answers 200 only after saving it, and a message that arrives twice is saved once.
"""

import base64
import http.client
import json
import logging
import queue
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from privatepartynet.identity import user_id_of
from privatepartynet.server import sniff_image
from privatepartynet.vault import VaultError

log = logging.getLogger("privatepartynet.dm")

MAX_ENVELOPE = 3 * 1024 * 1024  # a 2 MB picture as text, and the rest of the envelope
MAX_TEXT = 4000
MAX_IMAGE = 2 * 1024 * 1024
PER_MINUTE = 30  # messages accepted from one sender per minute
PER_ADDRESS_MINUTE = 60  # messages accepted from one network address per minute, whoever they claim to be
NEW_PER_HOUR = 20  # strangers (new contacts) accepted per hour from everywhere: a new identity costs nothing to make
MAX_CONNECTIONS = 32
TRY_SECONDS = 4.0  # how long one delivery attempt may take
FLUSH_SECONDS = 2.0  # how often the outbox is looked at
MAX_PER_FLUSH = 20
CLOCK_SKEW = 24 * 3600


def scope_for(peer_id):
    return f"dm:{peer_id}"


def _unverified():
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # the message is sealed to the recipient; the certificate proves nothing here
    return context


def _hex(value, size):
    return isinstance(value, str) and len(value) == size and all(c in "0123456789abcdef" for c in value)


def clean_text(value):
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable() or ch in " \n").strip()[:MAX_TEXT]


class DmService:
    def __init__(self, identity, store, name, certfile, keyfile, resolver=None, port=0, bind="0.0.0.0", auto=True):
        """`name` is a function that returns this person's display name. `resolver(user_id)` returns (address, port) for
        a person who is reachable right now, or None; saved addresses are tried after it. With auto=False nothing is
        delivered until flush() is called (tests drive delivery by hand)."""
        self.identity = identity
        self.store = store
        self._name = name
        self._resolver = resolver or (lambda user_id: None)
        self.events = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._recent = {}  # sender id or "@address" -> times of recent messages
        self._strangers = []  # times new contacts were accepted
        self._httpd = _DmServer((bind, port), self)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True, do_handshake_on_connect=False)
        self._threads = [threading.Thread(target=self._httpd.serve_forever, name="privatepartynet-dm", daemon=True)]
        if auto:
            self._threads.append(threading.Thread(target=self._outbox_loop, name="privatepartynet-dm-outbox", daemon=True))

    @property
    def port(self):
        return self._httpd.server_address[1]

    def start(self):
        for thread in self._threads:
            thread.start()

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self._wake.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        for thread in self._threads:
            thread.join(timeout=3)

    # --- sending ---------------------------------------------------------------------------------------------

    def card(self):
        return {**self.identity.card(self._name()), "port": self.port}

    def send(self, peer_id, text="", image=None):
        """Write a message to a contact. It is saved on this PC at once and delivered as soon as they can be reached.
        `image` is the bytes of a picture (PNG, JPEG, GIF or WebP, at most 2 MB) or None. Returns the message id."""
        contact = self.store.contact(peer_id)
        if contact is None:
            raise KeyError("That person is not in your contacts.")
        text = clean_text(text)
        picture = None
        if image is not None:
            if len(image) > MAX_IMAGE or sniff_image(image) is None:
                raise ValueError("Only PNG, JPEG, GIF and WebP pictures up to 2 MB can be sent.")
            picture = image
        if not text and picture is None:
            raise ValueError("There is nothing to send.")
        message_id = secrets.token_hex(8)
        now = time.time()
        body = {"text": text, "name": self._name(), "from": self.identity.user_id}
        payload = {"id": message_id, "ts": now, "text": text}
        if picture is not None:
            body["image"] = {"hash": self.store.put_blob(picture), "w": 0, "h": 0}
            payload["image"] = base64.b64encode(picture).decode("ascii")
        self.store.add_message(message_id, scope_for(peer_id), self.identity.user_id, body, ts=now)
        self.store.queue_outgoing(message_id, scope_for(peer_id), peer_id, payload)
        self._wake.set()
        return message_id

    def pending(self, peer_id):
        """Ids of messages to this person that have not been delivered yet."""
        return {item["id"] for item in self.store.due_outgoing(now=float("inf"), peer=peer_id)}

    def flush(self, peer_id=None):
        """Try to deliver what is waiting (to one person, or everyone). Returns how many were delivered."""
        delivered = 0
        for item in self.store.due_outgoing(peer=peer_id)[:MAX_PER_FLUSH]:
            if self._stop.is_set():
                break
            outcome = self._deliver(item)
            if outcome == "delivered":
                self.store.sent(item["id"])
                delivered += 1
            elif outcome == "refused":
                self.store.sent(item["id"])
                self.store.update_body(item["id"], item["scope"], {"refused": True})
                self.events.put(("refused", {"peer": item["peer"], "id": item["id"]}))
            else:
                self.store.failed(item["id"])
        return delivered

    def _addresses(self, peer_id):
        found = []
        live = self._resolver(peer_id)
        if live:
            found.append(tuple(live))
        contact = self.store.contact(peer_id) or {}
        if contact.get("addr") and contact.get("port"):
            saved = (contact["addr"], int(contact["port"]))
            if saved not in found:
                found.append(saved)
        return found

    def _deliver(self, item):
        contact = self.store.contact(item["peer"])
        if contact is None:
            return "refused"  # the contact was deleted; there is nobody to send it to any more
        plain = json.dumps(item["payload"], separators=(",", ":")).encode("utf-8")
        sealed = self.identity.seal_to(contact["dh"], plain, self._aad(contact["id"], self.identity.user_id))
        envelope = json.dumps({"v": 1, "from": self.card(), "to": contact["id"], "sealed": sealed.hex()}).encode("utf-8")
        for address, port in self._addresses(item["peer"]):
            connection = None
            try:
                connection = http.client.HTTPSConnection(address, port, timeout=TRY_SECONDS, context=_unverified())
                connection.request("POST", "/dm", body=envelope, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                response.read()
                if response.status == 200:
                    return "delivered"
                if response.status in (400, 403, 409, 413):
                    log.warning("A direct message was refused (%s)", response.status)
                    return "refused"  # trying again will not change it; the message stays saved here, marked
            except (OSError, http.client.HTTPException):
                continue
            finally:
                if connection is not None:
                    connection.close()
        return "unreachable"

    @staticmethod
    def _aad(to_id, from_id):
        return f"dm|{to_id}|{from_id}".encode("utf-8")

    def _outbox_loop(self):
        while not self._stop.is_set():
            self._wake.wait(FLUSH_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.flush()
            except Exception:
                log.exception("Delivering direct messages failed")

    # --- receiving -------------------------------------------------------------------------------------------

    def receive(self, envelope, source_address):
        """Take one envelope. Returns (status, message). The sender is told 200 only once the message is saved."""
        try:
            sender = envelope["from"]
            if envelope.get("v") != 1 or envelope.get("to") != self.identity.user_id:
                return 400, "This message is not for this PC."
            if not (_hex(sender.get("sign"), 64) and _hex(sender.get("dh"), 64) and _hex(sender.get("id"), 32)):
                return 400, "The sender's keys are not valid."
            if user_id_of(sender["sign"], sender["dh"]) != sender["id"]:
                return 403, "The sender's id does not match their keys."
            sealed = bytes.fromhex(envelope["sealed"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return 400, "That is not a direct message."
        if sender["id"] == self.identity.user_id:
            return 400, "That message came from this PC."
        if not self._allow(sender["id"], PER_MINUTE) or not self._allow("@" + str(source_address), PER_ADDRESS_MINUTE):
            return 429, "Too many messages; slow down."
        existing = self.store.contact(sender["id"])
        if existing is not None and (existing["sign"], existing["dh"]) != (sender["sign"], sender["dh"]):
            return 409, "The sender's keys are not the ones pinned for them."
        if existing is None and not self._allow_stranger():
            return 429, "Too many new people at once; try again later."
        try:
            plain = self.identity.open_from(sender["sign"], sealed, self._aad(self.identity.user_id, sender["id"]))
            payload = json.loads(plain)
            message_id = payload["id"]
            if not _hex(message_id, 16):
                raise ValueError("bad id")
            text = clean_text(payload.get("text"))
            picture = base64.b64decode(payload["image"], validate=True) if payload.get("image") else None
            if picture is not None and (len(picture) > MAX_IMAGE or sniff_image(picture) is None):
                raise ValueError("bad picture")
            if not text and picture is None:
                raise ValueError("empty")
            sent_at = float(payload.get("ts", 0))
        except (VaultError, ValueError, KeyError, TypeError):
            return 400, "That message could not be opened."
        now = time.time()
        if abs(sent_at - now) > CLOCK_SKEW:
            sent_at = now
        port = sender.get("port") if isinstance(sender.get("port"), int) and 0 < sender["port"] < 65536 else None
        name = "".join(ch for ch in str(sender.get("name") or "") if ch.isprintable()).strip()[:40] or "Someone"
        card = {"id": sender["id"], "name": name, "sign": sender["sign"], "dh": sender["dh"],
                "addr": source_address, "port": port}
        if existing is None:
            card["new"] = True  # someone not yet in the contacts: shown as a new contact until the person answers or adds them
        self.store.save_contact(card)
        body = {"text": text, "name": name, "from": sender["id"]}
        if picture is not None:
            body["image"] = {"hash": self.store.put_blob(picture), "w": 0, "h": 0}
        added = self.store.add_message(message_id, scope_for(sender["id"]), sender["id"], body, ts=sent_at)
        if added:
            self.events.put(("dm", {"peer": sender["id"], "id": message_id, "name": name}))
        return 200, "ok"

    def _allow(self, key, limit):
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._recent.get(key, []) if now - t < 60]
            if len(recent) >= limit:
                self._recent[key] = recent
                return False
            self._recent[key] = recent + [now]
            if len(self._recent) > 4096:  # many made-up senders must not grow this without end
                self._recent.pop(next(iter(self._recent)))
            return True

    def _allow_stranger(self):
        now = time.monotonic()
        with self._lock:
            self._strangers = [t for t in self._strangers if now - t < 3600]
            if len(self._strangers) >= NEW_PER_HOUR:
                return False
            self._strangers.append(now)
            return True


class _DmServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service):
        self.service = service
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, _DmHandler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # a flood of slow connections must not use up the PC
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        pass  # a peer that hangs up mid-request is not worth a traceback in a windowed app


class _DmHandler(BaseHTTPRequestHandler):
    server_version = "PrivatePartyNet"
    timeout = 15

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        if self.path != "/dm":
            self._answer(404, "Not found.")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_ENVELOPE:
            self._answer(413, "That message is too big.")
            return
        try:
            envelope = json.loads(self.rfile.read(length))
        except ValueError:
            self._answer(400, "That is not a direct message.")
            return
        status, message = self.server.service.receive(envelope, self.client_address[0])
        self._answer(status, message)

    def _answer(self, status, message):
        body = json.dumps({"ok": status == 200, "message": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
