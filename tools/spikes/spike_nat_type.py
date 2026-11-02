"""Spike 1: what kind of NAT is this PC behind?

Sends a STUN binding request from ONE UDP socket to several public STUN servers and compares the public port each
reports. The same port for every server means the NAT keeps one mapping per local socket ("easy", endpoint-independent):
hole punching works against it. Different ports mean a new mapping per destination ("hard"): punching needs the
birthday-paradox method or fails. Also lists the PC's addresses and whether IPv6 is global.

Bounded: every socket read has a 2 s timeout and the script ends after the servers are tried once. Read-only; it sends
a handful of 20-byte UDP packets to public STUN servers, which only report an address back.
"""
import os
import socket
import struct
import sys

SERVERS = [("stun.l.google.com", 19302), ("stun1.l.google.com", 19302), ("stun.cloudflare.com", 3478),
           ("stun.nextcloud.com", 3478)]
MAGIC = 0x2112A442


def binding(sock, host, port, family):
    infos = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)
    addr = infos[0][4]
    txid = os.urandom(12)
    sock.sendto(struct.pack("!HHI", 0x0001, 0, MAGIC) + txid, addr)
    data, _ = sock.recvfrom(2048)
    mtype, mlen, cookie = struct.unpack("!HHI", data[:8])
    pos = 20
    while pos + 4 <= 20 + mlen:
        atype, alen = struct.unpack("!HH", data[pos:pos + 4])
        body = data[pos + 4:pos + 4 + alen]
        if atype == 0x0020:  # XOR-MAPPED-ADDRESS
            port_x = struct.unpack("!H", body[2:4])[0] ^ (MAGIC >> 16)
            if body[1] == 1:
                ip = struct.unpack("!I", body[4:8])[0] ^ MAGIC
                return socket.inet_ntoa(struct.pack("!I", ip)), port_x, addr
            raw = bytes(a ^ b for a, b in zip(body[4:20], struct.pack("!I", MAGIC) + txid))
            return socket.inet_ntop(socket.AF_INET6, raw), port_x, addr
        pos += 4 + alen + (-alen % 4)
    return None


def probe(family):
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    try:
        sock.bind(("", 0))
    except OSError as error:
        print("  cannot bind:", error)
        return
    seen = []
    for host, port in SERVERS:
        try:
            result = binding(sock, host, port, family)
        except Exception as error:  # timeout, no route, DNS failure
            print(f"  {host}: {type(error).__name__}")
            continue
        if result:
            print(f"  {host}: sees {result[0]}:{result[1]}")
            seen.append(result)
    sock.close()
    ports = {port for _, port, _ in seen}
    ips = {ip for ip, _, _ in seen}
    if len(seen) < 2:
        print("  not enough answers to classify")
    elif len(ports) == 1:
        print(f"  RESULT: easy NAT (one mapping per socket); public {sorted(ips)[0]}:{sorted(ports)[0]}")
    else:
        print(f"  RESULT: HARD NAT (a new port per destination): ports {sorted(ports)}")
    return seen


def main():
    print("local addresses:")
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            print("  ", info[4][0])
    except OSError:
        pass
    print("IPv4 STUN:")
    v4 = probe(socket.AF_INET)
    print("IPv6 STUN:")
    probe(socket.AF_INET6)
    if v4:
        ip = v4[0][0]
        a, b = (int(x) for x in ip.split(".")[:2])
        shared = a == 100 and 64 <= b <= 127
        print(f"public IPv4 {ip}: {'inside 100.64.0.0/10 (carrier-grade NAT address)' if shared else 'not in the shared 100.64.0.0/10 range'}")


if __name__ == "__main__":
    sys.exit(main())
