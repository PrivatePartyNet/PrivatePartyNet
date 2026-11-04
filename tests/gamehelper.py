"""Test support for the game network: the real helper built from wghelper/ and started without the permission prompt
(its adapter in memory, so no administrator rights are needed), and paths between members in memory."""

import queue
import random
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import gohelper  # noqa: E402

_built = {}


def helper_program():
    """The helper, built once per test run. None when Go is not installed and no built copy is in assets."""
    if "program" not in _built:
        target = ROOT / "build" / "test" / gohelper.PROGRAM
        try:
            _built["program"] = gohelper.build(target)
        except RuntimeError:
            bundled = ROOT / "assets" / gohelper.PROGRAM
            _built["program"] = bundled if bundled.exists() else None
    return _built["program"]


def launch_plain(program, arguments, cwd):
    """Start the helper as a normal process, as Windows would after the prompt. With --memory-adapter it needs no rights."""
    process = subprocess.Popen([program, *arguments], cwd=cwd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    threading.Thread(target=process.wait, daemon=True).start()  # it ends with its link; collected when it does
    return None


class Hub:
    """Where FakePaths find each other. Delivery happens on the hub's own thread, as on a real network."""

    def __init__(self):
        self.managers = {}
        self._queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            path, datagram = self._queue.get()
            path._deliver(datagram)


class FakePath:
    """PeerPath's contract (pathway.py): peer_id, kind, send, set_receiver, set_on_change, close."""

    def __init__(self, manager, peer_id, kind):
        self.peer_id, self.kind = peer_id, kind
        self._manager = manager
        self._receiver = self._on_change = None
        self.closed = False
        self.candidates = []

    def send(self, datagram):
        if not self.closed and self.kind and len(datagram) <= 1400:
            self._manager._send(self, bytes(datagram))
        elif len(datagram) > 1400:
            self._manager.oversized += 1

    def set_receiver(self, fn):
        self._receiver = fn

    def set_on_change(self, fn):
        self._on_change = fn

    def close(self):
        self.closed = True
        if self._manager.paths.get(self.peer_id) is self:
            del self._manager.paths[self.peer_id]

    def _deliver(self, datagram):
        if self._receiver is not None and not self.closed:
            self._receiver(datagram)

    def _changed(self, kind):
        self.kind = kind
        if self._on_change is not None:
            self._on_change(kind)


class FakePaths:
    """PathManager's contract in memory: path_to, local_candidates, set_on_candidates, set_relay_config, stop."""

    def __init__(self, identity, hub, kind="lan", loss=0.0):
        self.user_id = identity if isinstance(identity, str) else identity.user_id
        self.hub, self.kind, self.loss = hub, kind, loss
        self.paths = {}
        self.relay_config = None
        self.on_candidates = None
        self.stopped = False
        self.oversized = 0
        hub.managers[self.user_id] = self

    def path_to(self, peer_id, candidates):
        path = self.paths.get(peer_id)
        if path is None or path.closed:
            path = self.paths[peer_id] = FakePath(self, peer_id, self.kind)
        path.candidates = list(candidates)
        return path

    def local_candidates(self):
        return [["lan", "192.0.2.1", 1000 + len(self.hub.managers)], ["key", self.user_id, ""]]

    def set_on_candidates(self, fn):
        self.on_candidates = fn

    def set_relay_config(self, config):
        self.relay_config = config

    def switch(self, kind, peer_id=None):
        for pid, path in list(self.paths.items()):
            if peer_id is None or pid == peer_id:
                path._changed(kind)

    def _send(self, path, datagram):
        other = self.hub.managers.get(path.peer_id)
        back = other.paths.get(self.user_id) if other is not None and not other.stopped else None
        if self.stopped or back is None or back.closed or (self.loss and random.random() < self.loss):
            return
        self.hub._queue.put((back, datagram))

    def stop(self):
        self.stopped = True
        for path in list(self.paths.values()):
            path.closed = True
        if self.hub.managers.get(self.user_id) is self:
            del self.hub.managers[self.user_id]
