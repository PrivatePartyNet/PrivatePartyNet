"""Serves one call on a fixed port for the browser end-to-end test, and gives up after a deadline.

Usage: py serve_for_e2e.py <port> <passcode> <seconds>

The end-to-end driver starts this, then kills it when the run ends. The deadline is a backstop in case the driver
dies first, so the server never outlives the test.
"""

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from privatepartynet.cert import ensure_certificate  # noqa: E402
from privatepartynet.frames import ScreenGrabber  # noqa: E402
from privatepartynet.server import Party  # noqa: E402


def main():
    port, passcode, seconds = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
    folder = tempfile.mkdtemp(prefix="privatepartynet-e2e-")
    certfile, keyfile = ensure_certificate(folder, ["localhost", "127.0.0.1"])
    share = Party(passcode, port, certfile, keyfile, party_name="End-to-end party", host_name="Host",
                  grabber=ScreenGrabber())
    share.start()
    print(f"READY {share.local_port}", flush=True)
    # Alex joins as a 3.3 app does (it can keep its link: heartbeat, grace, resume); Sam as an older app.
    print("JOIN Alex " + share.join_token("Alex", user="e2e-alex", caps=["link"]), flush=True)
    print("JOIN Sam " + share.join_token("Sam"), flush=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.5)
    share.stop()


if __name__ == "__main__":
    main()
