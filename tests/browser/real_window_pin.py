"""Party windows accept only the certificate their join checked, in the real window stack (pywebview, WebView2).

    py tests\\browser\\real_window_pin.py
    py tests\\browser\\real_window_pin.py --disable-gpu

Starts two parties in this process, both served with certificate A, and opens a real window on each the way the app
does (browser arguments and windowpins as main() sets them; the windows are hidden). The first is pinned to A and must
show the party's page; the second is pinned to another certificate, B, and must be refused with the page that says
why. --disable-gpu first sets WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS to that, as some other software on a PC does,
which is how the old way of accepting the certificate failed (a party window then showed the host's certificate).

Bounded: a watchdog ends the process after 90 seconds whatever happens.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

if "--disable-gpu" in sys.argv[1:]:
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--disable-gpu"

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402

from privatepartynet import windowpins  # noqa: E402
from privatepartynet.app import browser_arguments, webview_options  # noqa: E402
from privatepartynet.cert import ensure_certificate, pins_of_pem  # noqa: E402
from privatepartynet.server import Party  # noqa: E402


def main():
    threading.Timer(90, lambda: os._exit(3)).start()
    arguments = browser_arguments(os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"))
    if arguments:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = arguments
    else:
        os.environ.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
    windowpins.install()

    folder = Path(tempfile.mkdtemp(prefix="ppn-pin-"))
    cert_a, key_a = ensure_certificate(folder / "a", ["127.0.0.1"])
    cert_b, _ = ensure_certificate(folder / "b", ["127.0.0.1"])
    parties = [Party("000000", 0, cert_a, key_a, party_name="Pin test", host_name="Host") for _ in range(2)]
    for party in parties:
        party.start()
    good = f"https://127.0.0.1:{parties[0].port}/"
    bad = f"https://127.0.0.1:{parties[1].port}/"
    windowpins.pin(good, pins_of_pem(Path(cert_a).read_text(encoding="utf-8")))
    windowpins.pin(bad, pins_of_pem(Path(cert_b).read_text(encoding="utf-8")))
    windows = {"pinned to A": webview.create_window("A", url=good, hidden=True),
               "pinned to B": webview.create_window("B", url=bad, hidden=True)}
    results = {}

    def check():
        time.sleep(8)
        for name, window in windows.items():
            try:
                results[name] = window.evaluate_js("document.body ? document.body.innerText : ''") or ""
            except Exception as exc:  # noqa: BLE001 - reported below
                results[name] = f"(could not read the page: {exc})"
        for window in windows.values():
            window.destroy()

    webview.start(check, **webview_options(str(folder / "webview")))
    for party in parties:
        party.stop()
    passed = "Open this party from the PrivatePartyNet app" in results.get("pinned to A", "") and \
        "This party window was stopped" in results.get("pinned to B", "")
    print("browser arguments:", repr(os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")))
    for name, text in results.items():
        print(f"{name}: {text[:120]!r}")
    print("PASSED" if passed else "FAILED", flush=True)
    os._exit(0 if passed else 1)


if __name__ == "__main__":
    main()
