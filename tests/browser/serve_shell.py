"""Serves the app's own pages (the one window's shell) for the shell's browser test, and gives up after a deadline.
Usage: py serve_shell.py <seconds>
The test starts this and kills it when the run ends; the deadline is a backstop in case the test dies first.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from privatepartynet.shellserver import ShellServer  # noqa: E402


def main():
    seconds = float(sys.argv[1])
    shell = ShellServer().start()
    print(f"SHELL {shell.port}", flush=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.5)
    shell.stop()


if __name__ == "__main__":
    main()
