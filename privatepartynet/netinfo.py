"""Finds the addresses other PCs on the home network can use to reach this one."""

import socket

LOOPBACK = "127.0.0.1"


def lan_addresses():
    """Return this PC's IPv4 addresses, the one used for outside traffic first. Falls back to loopback."""
    found = []
    # A UDP connect sends no packets. It only asks the OS which interface would route toward the LAN.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        _, _, others = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        others = []
    for address in others:
        if address not in found and not address.startswith("127."):
            found.append(address)
    return found or [LOOPBACK]


def address_for(peer_ip, addresses=None):
    """The address of this PC on the same subnet as peer_ip, so the peer can reach it. Falls back to the first."""
    if peer_ip.startswith("127."):
        return LOOPBACK
    addresses = [a for a in (addresses or lan_addresses()) if a != LOOPBACK] or [LOOPBACK]
    prefix = peer_ip.rsplit(".", 1)[0]
    for address in addresses:
        if address.rsplit(".", 1)[0] == prefix:
            return address
    return addresses[0]
