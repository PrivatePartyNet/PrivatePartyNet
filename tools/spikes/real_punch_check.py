"""Two-network check of the punch-through: does a joiner reach a host whose router lets nothing in, with no relay?

Run the host half on one PC and the join half on another PC on a DIFFERENT internet connection (a phone hotspot is
enough), with nothing forwarded on either router. Outbound UDP only (STUN servers and the BitTorrent DHT).

    host PC:   py tools\\spikes\\real_punch_check.py host
               (prints a line starting "JOIN WITH:"; copy it to the other PC)
    other PC:  py tools\\spikes\\real_punch_check.py join <the line after JOIN WITH:>

The host half runs a throwaway party and waits up to 10 minutes; the join half gives up after 3. The join half prints
each step and ends with RESULT PASS when the party answered through the tunnel. Both report each side's NAT kind.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from privatepartynet import punch  # noqa: E402
from privatepartynet.cert import ensure_certificate  # noqa: E402
from privatepartynet.connect import _connect, certificate_fingerprint, stun_probe  # noqa: E402
from privatepartynet.identity import Identity  # noqa: E402
from privatepartynet.server import Party  # noqa: E402


def host():
    folder = tempfile.mkdtemp(prefix="privatepartynet-punch-check-")
    certfile, keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
    party = Party("punch-check", 0, certfile, keyfile, party_name="Punch check", host_name="Host", server_id="5eed5eed5eed5eed")
    party.start()
    me = Identity.create()
    secret = party.new_invite_secret()
    print("this PC's router:", stun_probe())
    runner = punch.HostPunch(me._sign, party.server_id, certfile, keyfile, party.port, party.invite_secrets, poll=5.0)
    print("public address for the tunnel:", runner.mapping)
    ticket = {"k": me.sign_public.hex(), "s": party.server_id, "r": secret,
              "f": certificate_fingerprint(Path(certfile).read_text(encoding="utf-8"))}
    print("JOIN WITH:", json.dumps(ticket, separators=(",", ":")))
    deadline = time.monotonic() + 600
    try:
        while time.monotonic() < deadline:
            if runner.seen:
                print("a joiner wrote to the mailbox:", list(runner.seen))
            if runner.tunnel.peers:
                print("a joiner came in through the tunnel:", runner.tunnel.peers)
            time.sleep(5)
    finally:
        runner.close()
        party.stop()


def join(ticket_text):
    ticket = json.loads(ticket_text)
    print("this PC's router:", stun_probe())
    started = time.monotonic()
    try:
        tunnel = punch.join_through(ticket["k"], ticket["s"], bytes.fromhex(ticket["r"]), ticket["f"], connect_seconds=150)
    except Exception as exc:
        print(f"FAIL after {time.monotonic() - started:.0f} s: {exc}")
        return 1
    print(f"tunnel open after {time.monotonic() - started:.0f} s on loopback port {tunnel.port}")
    connection = _connect("127.0.0.1", tunnel.port, ticket["f"], 10)
    connection.request("GET", "/ask?challenge=1")
    reply = connection.getresponse()
    body = reply.read()
    connection.close()
    tunnel.close()
    ok = reply.status == 200 and json.loads(body).get("server") == ticket["s"]
    print("the party answered through the tunnel:", reply.status, body[:80])
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "host":
        sys.exit(host() or 0)
    if len(sys.argv) >= 3 and sys.argv[1] == "join":
        sys.exit(join(" ".join(sys.argv[2:])))
    print(__doc__)
    sys.exit(2)
