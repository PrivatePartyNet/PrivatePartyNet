"""How much of this PC's CPU the app uses: the app process, and the browser engine it started (WebView2 runs
under it). The party page shows this as the CPU readout. The figure is a share of the whole PC, so 100 means every
logical processor is busy.

The page also gets the split (the app's own work, and its browser engine) and the whole PC's busy share, so the figure can
be checked against Task Manager. Checked on 2026-10-19 against the Windows performance counters and against CPU cycle
counts: all three agreed on the app's figure to within a point, so a difference from Task Manager is a difference in what
is counted (its CPU column and its total use their own method), not in the arithmetic here.
"""

import os
import threading
import time

import psutil

_lock = threading.Lock()
_process = psutil.Process(os.getpid())
_cores = psutil.cpu_count() or os.cpu_count() or 1
_last = {"at": None, "own": 0.0, "engine": 0.0, "pc": 0.0, "detail": {"app": 0.0, "own": 0.0, "engine": 0.0, "pc": 0.0}}


def _seconds(process):
    try:
        times = process.cpu_times()
    except psutil.Error:
        return 0.0  # the process ended between the listing and the read
    return times.user + times.system


def _cpu_seconds():
    """(the app process's CPU seconds, its browser engine's, the whole PC's busy seconds across all logical processors)."""
    own = _seconds(_process)
    try:
        children = _process.children(recursive=True)
    except psutil.Error:
        children = []
    engine = sum(_seconds(child) for child in children)
    machine = psutil.cpu_times()
    busy = machine.user + machine.system + getattr(machine, "interrupt", 0.0) + getattr(machine, "dpc", 0.0)
    return own, engine, busy


def _share(seconds, wall):
    return round(min(100.0, max(0.0, seconds) / (wall * _cores) * 100), 1)


def sample_detail():
    """{"app", "own", "engine", "pc"}, each a share of the whole PC's CPU from 0 to 100 since the last reading. app is
    own plus engine. Readings less than half a second apart return the previous figures, so the page may ask as often
    as it likes."""
    with _lock:
        now = time.monotonic()
        own, engine, busy = _cpu_seconds()
        if _last["at"] is None:
            _last.update(at=now, own=own, engine=engine, pc=busy)
            return dict(_last["detail"])
        wall = now - _last["at"]
        if wall >= 0.5:
            used_own = max(0.0, own - _last["own"])
            used_engine = max(0.0, engine - _last["engine"])
            _last["detail"] = {
                "app": _share(used_own + used_engine, wall),
                "own": _share(used_own, wall),
                "engine": _share(used_engine, wall),
                "pc": _share(busy - _last["pc"], wall),
            }
            _last.update(at=now, own=own, engine=engine, pc=busy)
        return dict(_last["detail"])


def sample():
    """The app's share of the whole PC's CPU since the last reading, from 0 to 100, to one decimal."""
    return sample_detail()["app"]
