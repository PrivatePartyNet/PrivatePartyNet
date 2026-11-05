"""Which certificate each party window may accept.

A party is served over HTTPS with the host's own self-signed certificate, which WebView2 does not trust. The app used
to turn certificate checks off for every window (--ignore-certificate-errors), so a window accepted any certificate
at all: whoever sat on the path between two PCs could stand in for the host and take the join token. Now each window
accepts exactly the certificate its join checked. Before a party window opens, its origin (https://host:port) is
pinned here to that certificate's pins (cert.py: the whole certificate's SHA-256 or its public key's). WebView2 asks
on every certificate it does not trust (ServerCertificateErrorDetected), and the answer is AlwaysAllow for a pinned
match and Cancel otherwise, with a page that says why. The control panel, About and How are files and the host's own
window is plain HTTP on the loopback address, so none of them has a certificate to check.
"""

import logging
import re
import threading
import urllib.parse

from privatepartynet.cert import pins_of_pem

log = logging.getLogger("privatepartynet.windowpins")

_pins = {}  # origin -> the set of pins a window on it accepts
_lock = threading.Lock()

REFUSED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>PrivatePartyNet</title>
<style>body { font-family: "Segoe UI", system-ui, sans-serif; margin: 0; min-height: 100vh; display: grid;
place-items: center; text-align: center; } p { max-width: 460px; line-height: 1.5; padding: 0 16px; }</style></head>
<body><p>This party window was stopped: the PC that answered is not the one PrivatePartyNet checked when you asked to
join (its certificate is different). Close this window and ask to join again. If it happens again, someone on the
network may be standing in for the host.</p></body></html>"""


def origin(url):
    """"https://host:port" for an HTTPS address (the host in lower case, IPv6 in brackets, 443 when none is given),
    else ""."""
    try:
        parts = urllib.parse.urlsplit(str(url))
        port = parts.port or 443
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    return f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"


def pin(url, pins):
    """A window on `url`'s origin accepts only a certificate with one of `pins` from now on (a later pin for the same
    origin replaces this one: a punched tunnel's local port can serve another party later)."""
    key = origin(url)
    if not key:
        return
    usable = {p for p in pins or () if isinstance(p, str) and re.fullmatch(r"[0-9a-f]{64}", p)}
    with _lock:
        _pins[key] = usable


def forget_all():
    with _lock:
        _pins.clear()


def allowed(url, pem):
    """True when the certificate `pem` (as WebView2 hands it over) is one pinned for `url`'s origin."""
    with _lock:
        wanted = set(_pins.get(origin(url), ()))
    if not wanted or not pem:
        return False
    try:
        return bool(wanted & set(pins_of_pem(str(pem))))
    except (ValueError, TypeError):
        return False


def on_certificate_error(browser, sender, args, delay=0.2):
    """pywebview's EdgeChrome.on_certificate_error, replaced by install(): allow a pinned certificate, refuse anything
    else and say why in the window."""
    from webview.platforms.edgechromium import CoreWebView2ServerCertificateErrorAction as Action

    uri = str(args.RequestUri)
    try:
        pem = str(args.ServerCertificate.ToPemEncoding())
    except Exception:  # noqa: BLE001 - a certificate WebView2 cannot hand over is not one to accept
        pem = ""
    if allowed(uri, pem):
        args.set_Action(Action.AlwaysAllow)
        return
    args.set_Action(Action.Cancel)
    log.warning("A party window refused %s: its certificate is not the one the join checked", origin(uri) or "a page")
    window = getattr(browser, "pywebview_window", None)
    refused = getattr(window, "ppn_refused", None)  # the one window: the shell shows it in that server's place
    if callable(refused):
        refused(origin(uri))
        return
    if window is not None:
        show = lambda: window.load_html(REFUSED_PAGE)  # noqa: E731 - after this handler returns, not inside it
        if delay:
            threading.Timer(delay, show).start()
        else:
            show()


def install():
    """Make every pywebview window ask the pins about a certificate WebView2 does not trust. pywebview hooks
    ServerCertificateErrorDetected only when IGNORE_SSL_ERRORS is set, and then answers with its own
    on_certificate_error, which allows everything; that answer is replaced here (pywebview is pinned in
    requirements.txt, and test_windowpins checks the hook is still there)."""
    import webview
    from webview.platforms import edgechromium

    webview.settings["IGNORE_SSL_ERRORS"] = True
    edgechromium.EdgeChrome.on_certificate_error = on_certificate_error
