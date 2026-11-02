"""Log PrivatePartyNet's CPU figure beside the operating system's own figures, a row every few seconds (reading every process takes a moment), so a disagreement with Task Manager
can be checked against data instead of memory.

    py tools\\cpu_audit.py --minutes 10 --out cpu_audit.csv

Each row has a timestamp (match it to the time on a Task Manager screenshot) and, as a share of the whole PC:
  chip     what the party window's CPU chip shows (the PrivatePartyNet process tree's processor time, as privatepartynet\\cpu.py counts it)
  cycles   the same tree measured in CPU cycles against the processor's nominal frequency (how "processor utility" counts)
  own      the PrivatePartyNet.exe process alone
  engine   its WebView2 processes
  pc       the whole PC's busy share, from the system's idle time
  clock    the average clock speed the tree ran at, in GHz
  other    the busiest process outside PrivatePartyNet, and its share
Read only. Ends by itself after --minutes (at most 60) and also if PrivatePartyNet is not running.
"""

import argparse
import csv
import ctypes
import sys
import time
import winreg
from ctypes import wintypes

import psutil

CORES = psutil.cpu_count() or 1
MAX_MINUTES = 60

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.QueryProcessCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulonglong)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def cycles_of(pid):
    handle = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return 0
    try:
        value = ctypes.c_ulonglong()
        return value.value if _k32.QueryProcessCycleTime(handle, ctypes.byref(value)) else 0
    finally:
        _k32.CloseHandle(handle)


def base_mhz():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return float(winreg.QueryValueEx(key, "~MHz")[0])
    except OSError:
        return 0.0


def find_roots():
    return [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "privatepartynet.exe"]


def read(roots):
    """(own seconds, engine seconds, tree cycles, {pid: (name, seconds)} of everyone else, the tree's process ids)"""
    mine, own, engine, cyc = {}, 0.0, 0.0, 0
    for root in roots:
        try:
            members = [root, *root.children(recursive=True)]
        except psutil.Error:
            members = [root]
        for proc in members:
            try:
                times = proc.cpu_times()
            except psutil.Error:
                continue
            seconds = times.user + times.system
            mine[proc.pid] = proc.name()
            cyc += cycles_of(proc.pid)
            if proc.pid == root.pid:
                own += seconds
            else:
                engine += seconds
    others = {}
    for proc in psutil.process_iter(["name"]):
        if proc.pid in mine or proc.pid == 0:
            continue
        try:
            times = proc.cpu_times()
            others[proc.pid] = (proc.info["name"], times.user + times.system)
        except psutil.Error:
            pass
    return own, engine, cyc, others, frozenset(mine)


def busy(times):
    return times.user + times.system + getattr(times, "interrupt", 0.0) + getattr(times, "dpc", 0.0)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--out", default="cpu_audit.csv")
    args = parser.parse_args()
    deadline = time.monotonic() + min(args.minutes, MAX_MINUTES) * 60
    nominal = base_mhz() * 1e6
    roots = find_roots()
    if not roots:
        print("PrivatePartyNet is not running.")
        return 1
    started, machine_start = time.monotonic(), psutil.cpu_times()
    previous = (started, read(roots), machine_start)
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(["time", "chip_pct", "cycles_pct", "own_pct", "engine_pct", "pc_pct", "clock_ghz", "other_process", "other_pct"])
        while time.monotonic() < deadline:
            time.sleep(1.0)
            roots = find_roots()
            if not roots:
                print("PrivatePartyNet stopped; ending.")
                break
            now, machine = time.monotonic(), psutil.cpu_times()  # taken together, before the slow read of every process
            snap = read(roots)
            then, before, machine0 = previous
            if snap[4] != before[4]:  # a process joined or left the tree, so its seconds and cycles cannot be compared
                out.writerow([time.strftime("%H:%M:%S"), "tree changed", "", "", "", "", "", "", ""])
                handle.flush()
                previous = (now, snap, machine)
                continue
            wall = now - then
            own, engine = snap[0] - before[0], snap[1] - before[1]
            cyc = snap[2] - before[2]
            top = max(((snap[3][pid][1] - before[3][pid][1], snap[3][pid][0]) for pid in snap[3] if pid in before[3]), default=(0.0, ""))
            share = lambda seconds: max(0.0, seconds) / (wall * CORES) * 100
            tree = own + engine
            out.writerow([
                time.strftime("%H:%M:%S"), f"{share(tree):.1f}",
                f"{(cyc / (wall * CORES * nominal) * 100) if nominal else 0:.1f}", f"{share(own):.1f}", f"{share(engine):.1f}",
                f"{share(busy(machine) - busy(machine0)):.1f}", f"{(cyc / tree / 1e9) if tree > 0 else 0:.2f}", top[1], f"{share(top[0]):.1f}",
            ])
            handle.flush()
            previous = (now, snap, machine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
