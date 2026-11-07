"""How each member's connection to the host is doing, so a bad link does not throw anyone out of the party.

The host sends every member a small ping over the party connection every PING_SECONDS, and the member's page answers
it. From the answers the host knows each member's round trip and loss, and when the member was last heard from. One
missed answer means nothing (Starlink drops out for a second or two now and then): a member is "unstable" after
UNSTABLE_SECONDS without a word, "reconnecting" after RECONNECTING_SECONDS, and is only taken out of the party when
nothing has come back for the whole grace period (the reconnect_grace_s setting). They are "ok" again after
GOOD_BEATS answers in a row. Everyone sees the state in the member list as

    "link": {"state": "ok" | "unstable" | "reconnecting", "rtt": ms or None, "loss": 0..1 over LOSS_WINDOW s,
             "since": unix time of the last change of state, "path": how they reach the host (when known)}

Nothing here touches the network; server.py drives it, with its own clock in tests.
"""

import ipaddress
from collections import deque

PING_SECONDS = 2.0
UNSTABLE_SECONDS = 4.0
RECONNECTING_SECONDS = 10.0
GOOD_BEATS = 3
LOSS_WINDOW = 30.0
RECONNECT_GRACE = 120.0
PATHS = ("direct-lan", "vpn", "direct-ipv6", "direct", "punched", "relay", "friend")
TUNNEL_WAYS = {}  # a tunnelled member's source address on the host (127.1.x.y) -> "punched" or "relay" (punch.py)


class LinkMeter:
    """One member's link, from the pings the host sends and the answers it gets. `now` is time.monotonic() (or a
    test's clock), `wall` the matching time.time() for "since"."""

    def __init__(self, now, wall, unstable=UNSTABLE_SECONDS, reconnecting=RECONNECTING_SECONDS, good=GOOD_BEATS,
                 window=LOSS_WINDOW):
        self.unstable, self.reconnecting, self.good, self.window = unstable, reconnecting, good, window
        self.state = "ok"
        self.since = wall
        self.heard = now  # when the member last answered (or connected)
        self.rtt = None  # smoothed round trip, seconds
        self.streak = 0  # answers in a row since the last silence
        self.path = None
        self._next = 0
        self._sent = {}  # ping id -> sent at
        self._late = {}  # ping id -> sent at, for pings counted as lost whose answer may still come
        self._results = deque()  # (sent at, answered) for pings old enough to count

    def ping(self, now):
        """A new ping is going out: its id, for the member to send back."""
        self._next += 1
        self._sent[self._next] = now
        self._settle(now)
        return self._next

    def pong(self, ping_id, now):
        """The member answered ping `ping_id`. False for an id never sent, or answered already."""
        try:
            ping_id = int(ping_id)
        except (TypeError, ValueError):
            return False
        late = ping_id not in self._sent
        sent = self._late.pop(ping_id, None) if late else self._sent.pop(ping_id)
        if sent is None:
            return False
        sample = max(0.0, now - sent)
        self.rtt = sample if self.rtt is None else self.rtt * 0.75 + sample * 0.25
        if not late:
            self._results.append((sent, True))
        # A late answer (a link with seconds of queueing) still shows the member is there; it stays lost for the loss.
        self.heard = now
        self.streak += 1
        return True

    def connected(self, now):
        """The member's event stream opened again: that counts as hearing from them, not as a good heartbeat."""
        self.heard = max(self.heard, now)

    def _settle(self, now):
        # A ping not answered within the dead time counts as lost; results older than the window are forgotten.
        late = max(self.unstable, 3 * (self.rtt or 0.0))
        for ping_id, sent in list(self._sent.items()):
            if now - sent > late:
                del self._sent[ping_id]
                self._results.append((sent, False))
                self._late[ping_id] = sent
        for ping_id in [p for p, sent in self._late.items() if now - sent > self.window]:
            del self._late[ping_id]
        while self._results and now - self._results[0][0] > self.window:
            self._results.popleft()

    @property
    def loss(self):
        if not self._results:
            return 0.0
        return round(sum(1 for _, answered in self._results if not answered) / len(self._results), 3)

    def update(self, now, wall):
        """Move the state on from how long the member has been silent. True when it changed."""
        self._settle(now)
        silent = now - self.heard
        state = self.state
        if silent >= self.reconnecting:
            state = "reconnecting"
        elif silent >= self.unstable:
            if state == "ok":
                state = "unstable"
        elif state != "ok" and self.streak >= self.good:
            state = "ok"
        if silent >= self.unstable:
            self.streak = 0  # a silence starts the count of good answers again
        if state == self.state:
            return False
        self.state, self.since = state, wall
        return True

    def silent_for(self, now):
        return now - self.heard

    def public(self):
        link = {"state": self.state, "rtt": None if self.rtt is None or self.state == "reconnecting"
                else int(round(self.rtt * 1000)), "loss": self.loss, "since": int(self.since)}
        if self.path:
            link["path"] = self.path
        return link


def path_of(address, tunnel_ways=None):
    """How a member at `address` (as the host sees it) reaches the host: "direct-lan", "direct-ipv6", "direct", or
    for a tunnelled member (127.1.x.y, punch.py) "punched" or "relay" from `tunnel_ways` (tunnel source -> way)."""
    try:
        ip = ipaddress.ip_address(str(address).split("%")[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip in ipaddress.ip_network("127.1.0.0/16"):
        return (tunnel_ways or {}).get(str(ip)) or "punched"
    if ip.is_loopback:
        return None  # the host's own window
    if ip.version == 6:
        return "direct-lan" if ip.is_private or ip.is_link_local else "direct-ipv6"
    if ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network("26.0.0.0/8") \
            or ip in ipaddress.ip_network("25.0.0.0/8"):
        return "vpn"  # a VPN both PCs are on (Tailscale, Radmin VPN, Hamachi)
    if ip.is_private:
        return "direct-lan"
    return "direct"
