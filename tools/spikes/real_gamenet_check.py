"""Real-adapter check of the game network. RUN ONLY WITH THE OWNER'S YES: Windows shows its permission prompt (UAC) once, and the
first run installs the Wintun driver (signed by WireGuard LLC) and adds a network adapter named PrivatePartyNet while it runs.

What it proves: a ping typed into Windows reaches a member through the real path and comes back.
  Windows ping -> PrivatePartyNet adapter (Wintun) -> elevated helper -> app -> game network router -> member link (TLS)
  -> a stand-in member in this script, which answers the ping -> the same way back -> "Reply from ..." in Windows.

Bounded: the whole check gives up after 120 seconds. Ending it closes the helper's connection, which ends the helper
and removes the adapter. Nothing is left running. Run from the project root:
    py tools\\spikes\\real_gamenet_check.py
"""

import ipaddress
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from privatepartynet.cert import ensure_certificate  # noqa: E402
from privatepartynet.gameclient import GameSession  # noqa: E402
from privatepartynet.gamenet import parse  # noqa: E402
from privatepartynet.identity import Identity  # noqa: E402
from privatepartynet.netlink import NetLink  # noqa: E402
from privatepartynet.server import Party  # noqa: E402

DEADLINE = 120
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f">{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def echo_reply(packet):
    """The answer to an ICMP echo request: addresses swapped, type 0, both checksums made again. None otherwise."""
    header = (packet[0] & 0x0F) * 4
    if packet[9] != 1 or packet[header] != 8:
        return None
    icmp = bytearray(packet[header:])
    icmp[0] = 0
    icmp[2:4] = b"\0\0"
    icmp[2:4] = struct.pack(">H", checksum(bytes(icmp)))
    ip = bytearray(packet[:header])
    ip[12:16], ip[16:20] = packet[16:20], packet[12:16]
    ip[8] = 64
    ip[10:12] = b"\0\0"
    ip[10:12] = struct.pack(">H", checksum(bytes(ip)))
    return bytes(ip) + bytes(icmp)


def main():
    started = time.monotonic()
    folder = tempfile.mkdtemp(prefix="privatepartynet-gamenet-check-")
    certfile, keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
    party = Party("check-only", 0, certfile, keyfile, party_name="Game network check", host_name="Host",
                  failed_login_delay=0, server_id="c0ffee00c0ffee00")
    party.start()
    member = Identity.create()
    session_token = party.redeem(party.join_token("Stand-in", user=member.user_id))
    party.attach(party.member_for(session_token))
    answers = {"count": 0}
    link_holder = {}

    def member_receives(packet):
        reply = echo_reply(packet)
        if reply:
            answers["count"] += 1
            link_holder["link"].send(reply)

    link = NetLink("127.0.0.1", party.port, party.fingerprint, member, member_receives)
    link_holder["link"] = link
    link.start()
    while link.status["state"] != "connected" and time.monotonic() - started < 20:
        time.sleep(0.1)
    if link.status["state"] != "connected":
        print("FAIL the stand-in member could not connect:", link.status)
        return 1
    member_address = link.status["address"]
    host_address, inbox, send = party.net_host()
    network = party.net_router().network
    session = GameSession(host_address, network, send, folder)

    def drain():
        while session.status["state"] not in ("off", "failed", "declined"):
            try:
                session.deliver(inbox.get(timeout=0.5))
            except Exception:
                continue

    threading.Thread(target=drain, daemon=True).start()
    session.start()
    print("asking Windows for permission (UAC)...")
    while session.status["state"] not in ("on", "failed", "declined") and time.monotonic() - started < DEADLINE:
        time.sleep(0.2)
    print("helper:", session.status)
    ok = False
    if session.status["state"] == "on":
        time.sleep(3)  # Windows takes a moment to bring the adapter up with its address
        adapter = subprocess.run(["netsh", "interface", "ipv4", "show", "addresses", "PrivatePartyNet"], capture_output=True,
                                 text=True, timeout=20, creationflags=NO_WINDOW)
        print(adapter.stdout.strip())
        time.sleep(5)
        profile = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "Get-NetConnectionProfile -InterfaceAlias PrivatePartyNet | Select-Object NetworkCategory | Format-List; "
                                  "Get-NetIPInterface -InterfaceAlias PrivatePartyNet -AddressFamily IPv4 | Select-Object InterfaceMetric | Format-List"],
                                 capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        print("network profile and metric:", " ".join(profile.stdout.split()))
        result = subprocess.run(["ping", "-n", "4", "-w", "2000", member_address], capture_output=True, text=True,
                                timeout=40, creationflags=NO_WINDOW)
        print(result.stdout.strip())
        ok = result.returncode == 0 and answers["count"] > 0
        print(f"the stand-in member answered {answers['count']} pings")
    session.stop()
    link.stop()
    party.stop()
    time.sleep(2)
    gone = subprocess.run(["netsh", "interface", "ipv4", "show", "addresses", "PrivatePartyNet"], capture_output=True, text=True,
                          timeout=20, creationflags=NO_WINDOW)
    print("adapter removed after the check:", gone.returncode != 0 or "PrivatePartyNet" not in gone.stdout)
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
