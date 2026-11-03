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


def ipv6_unicast():
    """This PC's IPv6 addresses that are in use, as [(address, preferred lifetime in seconds)], from Windows' own list
    (GetAdaptersAddresses), which also says how long each address stays preferred: a stable address keeps the router's
    lifetime, a temporary (privacy) one a day or less. Addresses still being checked or already deprecated are left out.
    None where that list cannot be read (not Windows), and the caller falls back to the host name's addresses."""
    import ctypes
    import sys

    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class SocketAddress(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]

    class Unicast(ctypes.Structure):
        pass

    Unicast._fields_ = [("Length", wintypes.ULONG), ("Flags", wintypes.DWORD), ("Next", ctypes.POINTER(Unicast)),
                        ("Address", SocketAddress), ("PrefixOrigin", ctypes.c_int), ("SuffixOrigin", ctypes.c_int),
                        ("DadState", ctypes.c_int), ("ValidLifetime", wintypes.ULONG),
                        ("PreferredLifetime", wintypes.ULONG)]

    class Adapter(ctypes.Structure):
        pass

    Adapter._fields_ = [("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD), ("Next", ctypes.POINTER(Adapter)),
                        ("AdapterName", ctypes.c_char_p), ("FirstUnicastAddress", ctypes.POINTER(Unicast))]

    preferred, skip = 4, 0x2 | 0x4 | 0x8  # IpDadStatePreferred; skip anycast, multicast and DNS servers
    try:
        get = ctypes.windll.iphlpapi.GetAdaptersAddresses
    except (AttributeError, OSError):
        return None
    size = wintypes.ULONG(16384)
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = get(socket.AF_INET6, skip, None, buffer, ctypes.byref(size))
        if result != 111:  # ERROR_BUFFER_OVERFLOW: size now says how much is needed
            break
    if result != 0:
        return None
    found = []
    adapter = ctypes.cast(buffer, ctypes.POINTER(Adapter))
    while adapter:
        unicast = adapter.contents.FirstUnicastAddress
        while unicast:
            entry = unicast.contents
            if entry.DadState == preferred and entry.Address.iSockaddrLength >= 24:
                raw = ctypes.string_at(entry.Address.lpSockaddr, 24)  # sockaddr_in6: family, port, flow, address
                found.append((socket.inet_ntop(socket.AF_INET6, raw[8:24]), int(entry.PreferredLifetime)))
            unicast = entry.Next
        adapter = adapter.contents.Next
    return found
