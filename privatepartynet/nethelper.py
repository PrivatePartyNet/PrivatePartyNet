"""The small helper that holds the game network adapter. It runs with administrator rights (Windows asks once when the
game network is turned on), because making a Wintun adapter needs them (measured: error 5 as a normal user).

It does one job: create or open the adapter named PrivatePartyNet, give it this server's address, and pass packets between it
and the app over a loopback connection authenticated with a one-time secret. It carries only IPv4 for the game network
(and broadcast and multicast); anything else Windows puts on the adapter is dropped here, so the adapter is never a
route to anywhere else.

Kill switches, all of them active at once:
  * the app's loopback connection closes (the app quit, crashed, or turned the game network off);
  * the app's process is gone (checked every two seconds);
  * 24 hours have passed.
On the way out it ends the Wintun session and closes the adapter, which removes it from Windows.

Started as: PrivatePartyNet.exe --net-helper --port P --secret-file F --address A --network N --parent PID
(from source: pythonw -m privatepartynet.nethelper with the same arguments). The secret is read from a file the app writes and
the helper deletes, so it never appears on a command line.
"""

import argparse
import ctypes
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from privatepartynet import gamenet
from privatepartynet.paths import asset_path

ADAPTER_NAME = "PrivatePartyNet"
TUNNEL_TYPE = "PrivatePartyNet"
RING = 0x400000  # 4 MiB, as in Wintun's own example; must be a power of two from 128 KiB to 64 MiB
MAX_SECONDS = 24 * 3600
ERROR_NO_MORE_ITEMS = 259
ERROR_HANDLE_EOF = 38
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def wintun_path():
    return asset_path("wintun.dll")


class Wintun:
    """The adapter through wintun.dll (the unmodified, signed DLL from wintun.net, used only through wintun.h)."""

    def __init__(self, dll_path, name=ADAPTER_NAME):
        self._dll = ctypes.WinDLL(str(dll_path), use_last_error=True)
        d = self._dll
        d.WintunCreateAdapter.restype = ctypes.c_void_p
        d.WintunCreateAdapter.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
        d.WintunOpenAdapter.restype = ctypes.c_void_p
        d.WintunOpenAdapter.argtypes = [wintypes.LPCWSTR]
        d.WintunCloseAdapter.argtypes = [ctypes.c_void_p]
        d.WintunStartSession.restype = ctypes.c_void_p
        d.WintunStartSession.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        d.WintunEndSession.argtypes = [ctypes.c_void_p]
        d.WintunGetReadWaitEvent.restype = wintypes.HANDLE
        d.WintunGetReadWaitEvent.argtypes = [ctypes.c_void_p]
        d.WintunReceivePacket.restype = ctypes.POINTER(ctypes.c_ubyte)
        d.WintunReceivePacket.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        d.WintunReleaseReceivePacket.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte)]
        d.WintunAllocateSendPacket.restype = ctypes.POINTER(ctypes.c_ubyte)
        d.WintunAllocateSendPacket.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        d.WintunSendPacket.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte)]
        self.name = name
        self._adapter = d.WintunOpenAdapter(name) or d.WintunCreateAdapter(name, TUNNEL_TYPE, None)
        if not self._adapter:
            raise OSError(ctypes.get_last_error(), "the game network adapter could not be made (administrator rights?)")
        self._session = d.WintunStartSession(self._adapter, RING)
        if not self._session:
            error = ctypes.get_last_error()
            d.WintunCloseAdapter(self._adapter)
            raise OSError(error, "the game network adapter could not be started")
        self._event = d.WintunGetReadWaitEvent(self._session)
        self._send_lock = threading.Lock()

    def read(self, timeout=0.5):
        """One packet Windows sent on the adapter, or None after `timeout` seconds."""
        size = wintypes.DWORD()
        pointer = self._dll.WintunReceivePacket(self._session, ctypes.byref(size))
        if not pointer:
            error = ctypes.get_last_error()
            if error == ERROR_NO_MORE_ITEMS:
                ctypes.windll.kernel32.WaitForSingleObject(self._event, int(timeout * 1000))
                return None
            raise EOFError(f"the adapter stopped ({error})")
        try:
            return ctypes.string_at(pointer, size.value)
        finally:
            self._dll.WintunReleaseReceivePacket(self._session, pointer)

    def write(self, packet):
        """Hand one packet to Windows as if it arrived on the adapter. Dropped when the ring is full."""
        with self._send_lock:
            pointer = self._dll.WintunAllocateSendPacket(self._session, len(packet))
            if not pointer:
                return False
            ctypes.memmove(pointer, packet, len(packet))
            self._dll.WintunSendPacket(self._session, pointer)
            return True

    def close(self):
        if self._session:
            self._dll.WintunEndSession(self._session)
            self._session = None
        if self._adapter:
            self._dll.WintunCloseAdapter(self._adapter)
            self._adapter = None


def configure(name, address, network, run=subprocess.run):
    """Give the adapter its address and metric 1, so games that pick the first network card, and broadcasts to
    255.255.255.255, go to the game network (see take_priority for other adapters at 1). No gateway is set: the adapter
    is never a route to the internet."""
    network = ipaddress.ip_network(network)
    commands = [
        ["netsh", "interface", "ipv4", "set", "address", f"name={name}", "source=static", f"address={address}",
         f"mask={network.netmask}", "gateway=none"],
        ["netsh", "interface", "ipv4", "set", "interface", f"interface={name}", "metric=1"],
    ]
    for command in commands:
        result = run(command, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        if result.returncode != 0:
            raise OSError(result.returncode, (result.stdout or result.stderr or "netsh failed").strip())
    return commands


def _powershell(script, run):
    return run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
               timeout=30, creationflags=NO_WINDOW)


def take_priority(name, state_file, run=subprocess.run):
    """Move every other connected adapter at metric 1 (Radmin VPN, for one) to 2 while the game network is on, so
    the game network's 1 wins (the game network takes priority while it is on). What each one had,
    including whether Windows chose its metric automatically, is written to `state_file` FIRST, so give_back() can put
    it back even after a crash. Returns the adapters moved."""
    restore_leftovers(state_file, run)
    result = _powershell("Get-NetIPInterface -AddressFamily IPv4 | Where-Object { $_.ConnectionState -eq 'Connected' "
                         f"-and $_.InterfaceAlias -ne '{name}' -and $_.InterfaceMetric -le 1 }} | Select-Object "
                         "InterfaceIndex, InterfaceAlias, InterfaceMetric, @{n='Automatic';e={[string]$_.AutomaticMetric}} "
                         "| ConvertTo-Json -Compress", run)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    found = json.loads(result.stdout)
    found = found if isinstance(found, list) else [found]
    moved = [{"index": int(i["InterfaceIndex"]), "alias": str(i["InterfaceAlias"]), "metric": int(i["InterfaceMetric"]),
              "automatic": str(i.get("Automatic", "")).lower() == "enabled"} for i in found]
    if not moved:
        return []
    Path(state_file).write_text(json.dumps(moved), encoding="utf-8")
    for item in moved:
        _powershell(f"Set-NetIPInterface -InterfaceIndex {item['index']} -AddressFamily IPv4 -InterfaceMetric 2", run)
    return moved


def give_back(moved, state_file, run=subprocess.run):
    """Put every adapter take_priority moved back exactly as it was, then forget the state file."""
    for item in moved:
        if item.get("automatic"):
            script = f"Set-NetIPInterface -InterfaceIndex {int(item['index'])} -AddressFamily IPv4 -AutomaticMetric Enabled"
        else:
            script = (f"Set-NetIPInterface -InterfaceIndex {int(item['index'])} -AddressFamily IPv4 "
                      f"-InterfaceMetric {int(item['metric'])}")
        try:
            _powershell(script, run)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        Path(state_file).unlink()
    except OSError:
        pass


def restore_leftovers(state_file, run=subprocess.run):
    """A helper that was killed outright could not give the metrics back; the next one does, before anything else."""
    path = Path(state_file)
    if not path.exists():
        return []
    try:
        moved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        moved = []
    give_back(moved if isinstance(moved, list) else [], state_file, run)
    return moved


def make_private(name, run=subprocess.run, tries=10, pause=1.0, sleep=time.sleep):
    """Mark the adapter's network Private, as Radmin VPN does its own (measured with Radmin VPN). A new adapter with no
    gateway is an unidentified network, which Windows treats as Public, and then the firewall blocks a game that hosts.
    Windows makes the network profile a few seconds after the address is set, so this tries again for a while.
    Returns True when it took. Never raises: the game network works without it, games just ask the firewall."""
    command = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               f"Set-NetConnectionProfile -InterfaceAlias '{name}' -NetworkCategory Private"]
    for _ in range(tries):
        try:
            result = run(command, capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0:
            return True
        sleep(pause)
    return False


def parent_alive(pid):
    """True while the process `pid` is running (Windows); always True elsewhere or for pid 0."""
    if not pid or sys.platform != "win32":
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        kernel32.CloseHandle(handle)


class _SocketStream:
    def __init__(self, sock):
        self._sock = sock

    def read(self, count):
        return self._sock.recv(count)

    def write(self, data):
        self._sock.sendall(data)

    def flush(self):
        pass


def serve(tun, sock, network, parent=0, max_seconds=MAX_SECONDS, alive=parent_alive):
    """Carry packets between the adapter and the app's socket until one of the kill switches trips. Returns why."""
    network = ipaddress.ip_network(network)
    stream = _SocketStream(sock)
    stop = threading.Event()
    reason = {}

    def end(why):
        reason.setdefault("why", why)  # the first reason is the real one; closing the socket wakes the others
        stop.set()

    def from_app():
        try:
            while not stop.is_set():
                packet = gamenet.read_frame(stream)
                if packet and gamenet.carried(packet, network):
                    tun.write(packet)
        except (EOFError, OSError, ValueError):
            end("the app closed the connection")
        finally:
            stop.set()

    def watchdog():
        started = time.monotonic()
        while not stop.wait(2.0):
            if not alive(parent):
                end("the app is gone")
            elif time.monotonic() - started > max_seconds:
                end("the time limit was reached")

    threads = [threading.Thread(target=from_app, daemon=True), threading.Thread(target=watchdog, daemon=True)]
    for thread in threads:
        thread.start()
    send_lock = threading.Lock()
    try:
        while not stop.is_set():
            try:
                packet = tun.read(timeout=0.5)
            except EOFError:
                end("the adapter stopped")
                break
            if packet and gamenet.carried(packet, network):
                with send_lock:
                    gamenet.write_frame(stream, packet)
    except OSError:
        end("the app closed the connection")
    finally:
        stop.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        for thread in threads:
            thread.join(timeout=3)
    return reason.get("why", "stopped")


def connect_to_app(port, secret, timeout=10.0):
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.sendall(secret.encode("ascii") + b"\n")
    sock.settimeout(None)
    return sock


def main(argv=None):
    parser = argparse.ArgumentParser(prog="PrivatePartyNet --net-helper")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--network", required=True)
    parser.add_argument("--parent", type=int, default=0)
    parser.add_argument("--state-file", default="")
    args = parser.parse_args([a for a in (argv if argv is not None else sys.argv[1:]) if a != "--net-helper"])
    secret_path = Path(args.secret_file)
    secret = secret_path.read_text(encoding="ascii").strip()
    try:
        secret_path.unlink()  # used once
    except OSError:
        pass
    state_file = args.state_file or str(Path(secret_path).with_name("net-metrics.json"))
    tun = Wintun(wintun_path())
    moved = []
    try:
        configure(ADAPTER_NAME, args.address, args.network)
        moved = take_priority(ADAPTER_NAME, state_file)
        threading.Thread(target=make_private, args=(ADAPTER_NAME,), daemon=True).start()
        sock = connect_to_app(args.port, secret)
        serve(tun, sock, args.network, parent=args.parent)
    finally:
        give_back(moved, state_file)
        tun.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
