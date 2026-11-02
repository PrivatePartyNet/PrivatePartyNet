"""A member's end of the game network: one TLS link to the host's /net, carrying packets both ways.

The link checks the host certificate against the pinned fingerprint, proves who this person is (a signature over a fresh
challenge and the certificate it checked, as the join does), then stays open. It reconnects by itself with growing pauses
(up to 30 seconds) until stop() is called, and ends for good when the host refuses it (not a member, or a guest).
"""

import json
import logging
import queue
import threading
import time

from privatepartynet import gamenet
from privatepartynet.connect import ConnectError, _connect
from privatepartynet.server import net_message

log = logging.getLogger("privatepartynet.netlink")

OUTBOX = 1024


class _SocketWriter:
    def __init__(self, sock):
        self._sock = sock

    def write(self, data):
        self._sock.sendall(data)

    def flush(self):
        pass


class NetLink:
    def __init__(self, address, port, fingerprint, identity, on_packet, timeout=5.0, keepalive=gamenet.KEEPALIVE_SECONDS,
                 max_backoff=30.0):
        self.address, self.port, self.fingerprint = address, port, fingerprint
        self.identity = identity
        self.on_packet = on_packet  # called with each packet from the host, on the link's thread
        self._timeout = timeout
        self._keepalive = keepalive
        self._max_backoff = max_backoff
        self._outbox = queue.Queue(maxsize=OUTBOX)
        self._stop = threading.Event()
        self._thread = None
        self.status = {"state": "idle", "message": "", "address": None, "network": None, "host": None}

    def start(self):
        self._thread = threading.Thread(target=self._run, name="privatepartynet-netlink", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def send(self, packet):
        """Queue one packet for the host. Dropped (not blocked on) when the link is down or behind."""
        if self.status["state"] != "connected":
            return False
        try:
            self._outbox.put_nowait(packet)
            return True
        except queue.Full:
            return False

    # --- the link ------------------------------------------------------------------------------------------------

    def _set(self, state, message="", **extra):
        self.status = {**self.status, "state": state, "message": message, **extra}

    def _run(self):
        pause = 1.0
        while not self._stop.is_set():
            self._set("connecting", "Connecting to the host...")
            try:
                self._session()
                pause = 1.0
            except PermissionError as exc:
                self._set("refused", str(exc))
                return
            except (OSError, ConnectError, ValueError, EOFError) as exc:
                self._set("retrying", f"Lost the host ({exc}); trying again.")
            if self._stop.wait(pause):
                break
            pause = min(self._max_backoff, pause * 2)
        self._set("stopped", "")

    def _session(self):
        connection = _connect(self.address, self.port, self.fingerprint, self._timeout)
        try:
            connection.request("GET", "/ask?challenge=1")
            reply = connection.getresponse()
            challenge = json.loads(reply.read())
        finally:
            connection.close()
        nonce = challenge["nonce"]
        connection = _connect(self.address, self.port, self.fingerprint, self._timeout)
        try:
            body = json.dumps({"card": self.identity.card(), "nonce": nonce,
                               "sig": self.identity.sign(net_message(connection.seen_fingerprint, nonce)).hex()})
            connection.request("POST", "/net", body=body, headers={"Content-Type": "application/json"})
            sock = connection.sock  # http.client gives the socket to the response once it sees the link stays open
            response = connection.getresponse()
            if response.status == 403:
                raise PermissionError(response.read().decode("utf-8", "replace") or "The host refused the game network.")
            if response.status != 200:
                raise ConnectError(f"the host answered {response.status}")
            hello = json.loads(response.readline())
            # The host sends a keep-alive every KEEPALIVE_SECONDS; three missed in a row means the link is dead.
            sock.settimeout(gamenet.KEEPALIVE_SECONDS * 3)
            self._set("connected", "", address=hello["address"], network=hello["network"], host=hello["host"])
            self._pump(response, _SocketWriter(sock))
        finally:
            connection.close()
            try:
                sock.close()
            except (NameError, UnboundLocalError, OSError):
                pass

    def _pump(self, reader, writer):
        done = threading.Event()

        def receive():
            try:
                while not done.is_set() and not self._stop.is_set():
                    packet = gamenet.read_frame(reader)
                    if packet:
                        try:
                            self.on_packet(packet)
                        except Exception:
                            log.exception("Handing a game network packet on failed")
            except (EOFError, OSError, ValueError):
                pass
            finally:
                done.set()

        thread = threading.Thread(target=receive, name="privatepartynet-netlink-read", daemon=True)
        thread.start()
        last = time.monotonic()
        try:
            while not done.is_set() and not self._stop.is_set():
                try:
                    packet = self._outbox.get(timeout=0.5)
                except queue.Empty:
                    if time.monotonic() - last >= self._keepalive:
                        gamenet.write_frame(writer, b"")
                        last = time.monotonic()
                    continue
                gamenet.write_frame(writer, packet)
                last = time.monotonic()
        finally:
            done.set()
            thread.join(timeout=2)
        if not self._stop.is_set():
            raise EOFError("the host closed the link")
