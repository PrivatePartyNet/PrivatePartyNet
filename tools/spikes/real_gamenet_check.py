"""Real-adapter check of the game network. RUN ONLY WITH THE OWNER'S YES: Windows shows its permission prompt (UAC) once, and the
first run installs the Wintun driver (signed by WireGuard LLC) and adds a network adapter named PrivatePartyNet while it runs.

What it proves, with this PC as the host on its real adapter and a stand-in member in this script:
  1. A ping typed into Windows reaches the member over WireGuard and comes back:
     Windows ping -> PrivatePartyNet adapter (Wintun) -> helper (WireGuard seals it) -> app -> the path to the member
     -> the member's app -> the member's helper (WireGuard opens it) -> the stand-in member, which answers the ping
     -> the same way back -> "Reply from ..." in Windows.
  2. A broadcast a game sends on the adapter (UDP to the network's broadcast address and to 255.255.255.255) reaches
     the member, through the helper's fan-out.
  3. The adapter is the game network's: its address, metric 1, the network marked Private, and its packet size.
The stand-in member's helper keeps its adapter in memory (no second permission prompt), and the path between the two
is in memory, as on one PC both ends would be. The ping times are printed for comparison with the earlier design.

Bounded: the whole check gives up after 120 seconds. Ending it closes the helpers' connections, which ends both helpers
and removes the adapter. Nothing is left running. Build the helper first, then run from the project root:
    py tools\\gohelper.py
    py tools\\spikes\\real_gamenet_check.py
"""

import queue
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from gamehelper import FakePaths, Hub, launch_plain  # noqa: E402
from privatepartynet import gameclient  # noqa: E402
from privatepartynet.cert import ensure_certificate  # noqa: E402
from privatepartynet.gameclient import GameMesh, GameSession  # noqa: E402
from privatepartynet.gamenet import new_private_key, open_roster, public_key  # noqa: E402
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


def powershell(script):
    return subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=30,
                          creationflags=NO_WINDOW).stdout


def main():
    started = time.monotonic()
    if not gameclient.helper_path().exists():
        print("FAIL build the helper first: py tools\\gohelper.py")
        return 1
    folder = Path(tempfile.mkdtemp(prefix="privatepartynet-gamenet-check-"))
    certfile, keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
    owner = Identity.create()
    party = Party("check-only", 0, certfile, keyfile, party_name="Game network check", host_name="Host",
                  failed_login_delay=0, server_id="c0ffee00c0ffee00", host_user_id=owner.user_id, signer=owner.sign)
    party.start()
    hub = Hub()

    # the stand-in member: joined, its window open, its helper in memory, answering pings and noting broadcasts
    member = Identity.create()
    party.attach(party.member_for(party.redeem(party.join_token("Stand-in", user=member.user_id))))
    member_private = new_private_key()
    member_paths = FakePaths(member, hub)
    broadcasts = queue.Queue()
    answered = {"count": 0}
    holder = {}

    def member_receives(packet):
        reply = echo_reply(packet)
        if reply:
            answered["count"] += 1
            holder["session"].put_packet(reply)
        elif packet[9] == 17:
            broadcasts.put(packet)

    def member_roster(roster, relay):
        if "mesh" not in holder:
            session = GameSession(link.status["address"], link.status["network"], member_private, folder,
                                  launch=launch_plain, memory_adapter=True, on_packet=member_receives)
            holder["session"], holder["mesh"] = session, GameMesh(member.user_id, member_paths, session)
            session.start()
        holder["mesh"].apply(roster)

    link = NetLink("127.0.0.1", party.port, party.fingerprint, member, public_key(member_private), member_roster,
                   host_sign=owner.sign_public.hex(), server_id=party.server_id)
    link.start()

    # this PC as the host, on its real adapter
    host_private = new_private_key()
    host_paths = FakePaths(owner, hub)
    address = party.net_host(public_key(host_private), host_paths.local_candidates())
    network = party.net_directory().network
    session = GameSession(address, network, host_private, folder)
    mesh = GameMesh(owner.user_id, host_paths, session)
    stop = threading.Event()

    def follow():
        version = None
        while not stop.is_set():
            current = party.net_wait(version, 0.5)
            if current != version:
                version = current
                mesh.apply(open_roster(party.net_roster(party.net_host_user)))

    threading.Thread(target=follow, daemon=True).start()
    session.start()
    print("asking Windows for permission (UAC)...")
    while session.status["state"] not in ("on", "failed", "declined") and time.monotonic() - started < DEADLINE:
        time.sleep(0.2)
    print("helper:", session.status)
    ok = False
    member_address = link.status["address"]
    if session.status["state"] == "on" and member_address:
        time.sleep(5)  # Windows takes a moment to bring the adapter up with its address and to make its profile
        print(subprocess.run(["netsh", "interface", "ipv4", "show", "addresses", "PrivatePartyNet"], capture_output=True,
                             text=True, timeout=20, creationflags=NO_WINDOW).stdout.strip())
        print("profile, metric and packet size:", " ".join(powershell(
            "Get-NetConnectionProfile -InterfaceAlias PrivatePartyNet | Select-Object NetworkCategory | Format-List; "
            "Get-NetIPInterface -InterfaceAlias PrivatePartyNet -AddressFamily IPv4 | Select-Object InterfaceMetric, NlMtu "
            "| Format-List").split()))
        result = subprocess.run(["ping", "-n", "20", "-w", "2000", member_address], capture_output=True, text=True,
                                timeout=60, creationflags=NO_WINDOW)
        print(result.stdout.strip())
        pinged = result.returncode == 0 and answered["count"] > 0
        print(f"the stand-in member answered {answered['count']} pings")

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sender.bind((str(address), 0))
        for target in (str(network.broadcast_address), "255.255.255.255"):
            sender.sendto(b"lobby? " + target.encode(), (target, 47624))
        sender.close()
        seen = set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(seen) < 2:
            try:
                packet = broadcasts.get(timeout=0.5)
            except queue.Empty:
                continue
            if b"lobby? " in packet:
                seen.add(packet.split(b"lobby? ")[1].decode())
        print("broadcasts that reached the member:", sorted(seen))
        ok = pinged and len(seen) == 2
    stop.set()
    session.stop()
    if "session" in holder:
        holder["session"].stop()
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
