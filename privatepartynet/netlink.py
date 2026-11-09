"""A member's end of the game network's link to the host: one TLS connection to the host's /net that keeps this member
on the list of who is on, and brings the list (signed by the host) every time it changes.

No game traffic travels on it; that goes straight between members over WireGuard (gamenet.py). The link checks the host
certificate against the pinned fingerprint, proves who this person is (a signature over a fresh challenge, the
certificate it checked and this PC's WireGuard key for the server, as the join does), then stays open. It reconnects by
itself with growing pauses (up to 30 seconds) until stop() is called, and ends for good when the host refuses it (not
a member, or a guest).
"""

import json
import logging
import socket
import threading

from privatepartynet import gamenet
from privatepartynet.connect import ConnectError, _connect
from privatepartynet.server import net_message

log = logging.getLogger("privatepartynet.netlink")


class NetLink:
    def __init__(self, address, port, fingerprint, identity, key, on_roster, candidates=(), host_sign="", server_id="",
                 timeout=5.0, max_backoff=10.0):
        self.address, self.port, self.fingerprint = address, port, fingerprint
        self.identity = identity
        self.key = key  # this PC's WireGuard public key for the server
        self.on_roster = on_roster  # called with (list, relay settings) each time the list changes, on the link's thread
        self.host_sign, self.server_id = host_sign, server_id
        self._candidates = gamenet.clean_candidates(list(candidates))
        self._timeout = timeout
        self._max_backoff = max_backoff
        self._stop = threading.Event()
        self._sock = None
        self._send_lock = threading.Lock()
        self._thread = None
        self.status = {"state": "idle", "message": "", "address": None, "network": None, "host": None}

    def start(self):
        self._thread = threading.Thread(target=self._run, name="privatepartynet-netlink", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # close() alone waits for the response's own reference to let go
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def set_candidates(self, candidates):
        """This PC's ways to be reached changed: tell the host, which sends everyone the new list."""
        self._candidates = gamenet.clean_candidates(list(candidates))
        sock = self._sock
        if sock is None:
            return False  # the next connection carries them
        try:
            with self._send_lock:
                sock.sendall(json.dumps({"candidates": self._candidates}).encode("utf-8") + b"\n")
            return True
        except OSError:
            return False

    # --- the link ------------------------------------------------------------------------------------------------

    def _set(self, state, message="", **extra):
        self.status = {**self.status, "state": state, "message": message, **extra}

    def _run(self):
        """Keep the link up. After a drop it tries again at once-ish and then less often (partylink.backoff: 0.5 s,
        1, 2, 4, 8, then 10 s, with jitter); the game network adapter and its tunnels stay as they are meanwhile,
        and the host keeps this PC on the list through its reconnect grace."""
        from privatepartynet.partylink import backoff

        attempt = 0
        while not self._stop.is_set():
            self._set("connecting", "Connecting to the host...")
            try:
                self._session()
                attempt = 0
            except PermissionError as exc:
                self._set("refused", str(exc))
                return
            except (OSError, ConnectError, ValueError, EOFError, gamenet.NetError) as exc:
                if self._stop.is_set():
                    break
                self._set("retrying", f"Lost the host ({exc}); trying again.")
                log.info("The game network link to the host dropped (%s); trying again", exc)
            if self._stop.wait(min(self._max_backoff, backoff(attempt))):
                break
            attempt += 1
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
        sock = None
        try:
            body = json.dumps({"card": self.identity.card(), "nonce": nonce, "key": self.key,
                               "candidates": self._candidates,
                               "sig": self.identity.sign(net_message(connection.seen_fingerprint, nonce, self.key)).hex()})
            connection.request("POST", "/net", body=body, headers={"Content-Type": "application/json"})
            sock = connection.sock  # http.client gives the socket to the response once it sees the link stays open
            response = connection.getresponse()
            if response.status == 403:
                raise PermissionError(response.read().decode("utf-8", "replace") or "The host refused the game network.")
            if response.status != 200:
                raise ConnectError(f"the host answered {response.status}: "
                                   f"{response.read().decode('utf-8', 'replace')[:200]}")
            hello = json.loads(response.readline())
            # The host sends a line every KEEPALIVE_SECONDS; three missed in a row means the link is dead.
            sock.settimeout(gamenet.KEEPALIVE_SECONDS * 3)
            self._sock = sock
            self._set("connected", "", address=hello["address"], network=hello["network"], host=hello["host"])
            self._follow(response, hello["network"])
        finally:
            self._sock = None
            connection.close()
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _follow(self, response, network):
        newest = -1
        while not self._stop.is_set():
            line = response.readline()
            if not line:
                raise EOFError("the host closed the link")
            message = json.loads(line)
            if not message:
                continue  # a keep-alive
            roster = gamenet.open_roster(message, self.host_sign, self.server_id, network)
            if roster["seq"] < newest:
                continue  # an older list than one already followed; the same one again brings fresh relay settings
            newest = roster["seq"]
            try:
                self.on_roster(roster, message.get("relay"))
            except Exception:
                log.exception("Following the game network's list of members failed")
