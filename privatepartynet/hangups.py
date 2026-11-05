"""The other end of a connection going away, told apart from real faults.

A browser or a member hangs up all the time: a party window closes, Go Live restarts its frame feed at another size, a
member's network drops. Windows reports that as ConnectionAbortedError (WinError 10053), ConnectionResetError (10054),
a socket already shut down (10058), a broken pipe, or a TLS connection cut short. None of that is a problem to show
anyone: the connection is closed and the rest goes on. HangupsLogged makes a request handler (http.server) close such a
connection and note it in the log at INFO, saying which connection it was.
"""

import logging
import ssl

HANGUP_WINERRORS = frozenset({10053, 10054, 10058})
HANGUP_TYPES = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, ssl.SSLEOFError, ssl.SSLZeroReturnError)


def is_hangup(exc):
    """True when `exc` only means the other end went away."""
    if isinstance(exc, HANGUP_TYPES):
        return True
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in HANGUP_WINERRORS


def describe(exc):
    """A short name for a hangup, for the log: "WinError 10053" or the exception's own name."""
    code = getattr(exc, "winerror", None)
    return f"WinError {code}" if code else type(exc).__name__


class HangupsLogged:
    """A mixin for BaseHTTPRequestHandler: a hangup while reading or answering a request closes the connection and is
    logged at INFO under `hangup_log`, naming `hangup_what` (which server), the client and the request."""

    hangup_log = logging.getLogger("privatepartynet.connections")
    hangup_what = "a"

    def handle(self):
        try:
            super().handle()
        except Exception as exc:
            if not is_hangup(exc):
                raise
            self._log_hangup(exc)
            self.close_connection = True

    def finish(self):
        try:
            super().finish()
        except Exception as exc:
            if not is_hangup(exc):
                raise
            self._log_hangup(exc)

    def _log_hangup(self, exc):
        request = getattr(self, "requestline", "") or "before a request"
        self.hangup_log.info("The %s connection from %s closed while it was being answered (%s; %s)", self.hangup_what,
                             self.client_address[0], request[:120], describe(exc))
