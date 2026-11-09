"""Joining with an invite between two PCs that are both behind carrier-grade NAT, as in the user's real 3.1.0 run: on
Starlink, an invite with two private IPv4 addresses, and then "the host did not answer through the routers within 30
seconds (TimeoutError)", with nothing about it in the log.

What went wrong there, and what these tests hold to:

* The joiner gave the host 30 seconds (8 on IPv6) to open its side, but a 3.1 host read its invites' mailboxes on the
  DHT one after another, up to 20 of them at up to 20 seconds each, with 15 seconds between rounds: it usually noticed
  the joiner after the joiner had given up. Now the host reads every mailbox at once every few seconds, and the joiner
  keeps its side open for PUNCH_SECONDS, starting a fresh handshake the moment the host's first probe arrives.
* An invite made in the first seconds of a party, before the router check was in, carried only the LAN addresses: no
  IPv6. It now carries the PC's global IPv6 addresses from the start.
* The game network's own adapter address went into invites (it is reachable only through the game network), and a VPN
  adapter's (Radmin VPN, Hamachi, ...) could be missing. A host also seen in the Nearby list is tried there first.
* Every step is in the log, on both sides, without the password or keys; the message the person sees says what was
  tried and what was missing, in words.
"""

import base64
import hashlib
import ipaddress
import json
import logging
import socket
import threading
import time
import unittest
import zlib
from unittest import mock

import natsim
from privatepartynet import connect, punch
from privatepartynet.dht import Dht
from privatepartynet.identity import Identity
from privatepartynet.netinfo import lan_addresses
from privatepartynet.punch import HostPunch, PunchError, join_through
from test_dht import FakeNode
from test_punch import Base

PASSWORD = "river-stone-42"


def dual_stack_socket():
    """punch._v6_socket, but dual-stack, so natsim can carry it (see natsim: adopted IPv6 sockets)."""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", 0))
    return sock


def invite_of_3_0(candidates, fingerprint, secret, host_sign, server_id="feedfacecafebeef"):
    """An invite exactly as PrivatePartyNet 3.0 wrote it (git show v3.0.0-alpha.1:privatepartynet/connect.py): no
    version, no relays. 3.1 wrote the same, with "y" (relays) when there were any."""
    data = {"v": 1, "n": "Heroes night", "p": PASSWORD, "c": candidates, "f": fingerprint, "s": server_id,
            "k": host_sign, "r": secret}
    packed = zlib.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), 9)
    return "ppn1-" + base64.urlsafe_b64encode(packed + hashlib.sha256(packed).digest()[:3]).decode("ascii").rstrip("=")


class RendezvousBase(Base):
    def setUp(self):
        super().setUp()
        from privatepartynet import dht as dht_module

        dht_module._known.clear()
        self.network = []
        for _ in range(12):
            self.network.append(FakeNode(self.network))
        for node in self.network:
            self.addCleanup(node.close)
        self.internet = natsim.Internet()
        self.addCleanup(self.internet.close)
        self.stun = self.internet.stun_servers(2)
        self.host_identity = Identity.create()
        self.secret = b"invite-secret-07"

    def dht(self):
        return Dht(bootstrap=[self.network[0].address], timeout=0.5, deadline=6.0)

    def starlink_host(self, v6=natsim.V6_STATEFUL, poll=0.5):
        """A host on Starlink: IPv4 behind carrier-grade NAT (a new port for every destination) and a global IPv6
        address behind a firewall that lets in only answers to what the PC sent, which cannot be changed."""
        v4, v6 = self.internet.router(natsim.SYMMETRIC), self.internet.router(v6)
        with v4.adopting(), v6.adopting(), mock.patch.object(punch, "_v6_socket", dual_stack_socket):
            host = HostPunch(self.host_identity._sign, "feedfacecafebeef", self.certfile, self.keyfile,
                             self.party.port, lambda: [self.secret], dht_factory=self.dht, stun_servers=self.stun,
                             refresh=1.0, poll=poll, ipv6=lambda: [v6.mapped_address])
        self.addCleanup(host.close)
        return host, v4, v6

    def starlink_join(self, v6=natsim.V6_STATEFUL, connect_seconds=30, relays=()):
        v4, v6 = self.internet.router(natsim.SYMMETRIC), self.internet.router(v6)
        with v4.adopting(), v6.adopting(), mock.patch.object(punch, "_v6_socket", dual_stack_socket):
            client = join_through(self.host_identity.sign_public.hex(), "feedfacecafebeef", self.secret,
                                  self.fingerprint, dht_factory=self.dht, stun_servers=self.stun,
                                  connect_seconds=connect_seconds, find_seconds=10, relays=relays,
                                  ipv6=lambda: [v6.mapped_address])
        self.addCleanup(client.close)
        return client, v6


class StarlinkTests(RendezvousBase):
    """Both PCs on Starlink, no relay. IPv4 cannot get through two such routers; IPv6 can, because both send at once,
    which opens both firewalls."""

    def test_the_hosts_record_carries_its_ipv6_address(self):
        host, _, v6 = self.starlink_host()
        self.assertEqual(host.mapping[2], "hard", "Starlink's carrier-grade NAT gives a new port for every destination")
        self.assertEqual(host.mapping6, (v6.mapped_address, host.sock6.getsockname()[1]))
        time.sleep(2.0)  # published
        dht = self.dht()
        self.addCleanup(dht.close)
        found = dht.get_mutable(self.host_identity.sign_public, punch.host_salt("feedfacecafebeef"))
        address, port = punch.read_record6(found[1])
        self.assertEqual((ipaddress.ip_address(address), port), (ipaddress.ip_address(host.mapping6[0]), host.mapping6[1]))
        self.assertTrue(punch.record_version(found[1]), "3.2 on says which version wrote the record")

    def test_two_starlink_pcs_join_over_ipv6_through_both_firewalls(self):
        host, _, host_v6 = self.starlink_host()
        time.sleep(2.0)
        client, joiner_v6 = self.starlink_join()
        self.assertEqual(client.kind, "ipv6")
        self.assertEqual(self.through(client.port, "GET", "/ask?challenge=1")[0], 200)
        (peer,) = host.tunnel6.peers
        self.assertEqual(ipaddress.ip_address(peer[0]), ipaddress.ip_address(joiner_v6.mapped_address),
                         "the host knows which joiner a tunnel is from (3.1 saw 0.0.0.0 for every one)")
        refused = [why for why, _, _ in host_v6.dropped] + [why for why, _, _ in joiner_v6.dropped]
        self.assertTrue(refused, "the firewalls were really there, and let in only what each side had sent to")

    def test_a_host_that_notices_the_joiner_late_is_still_reached(self):
        """3.1 tried IPv6 for 8 seconds, then refused because both IPv4 routers are strict: a host that took longer to
        read its mailbox (it always did, on the real DHT) was never reached."""
        host, _, _ = self.starlink_host(poll=12.0)
        time.sleep(2.0)
        started = time.monotonic()
        client, _ = self.starlink_join(connect_seconds=40)
        self.assertEqual(client.kind, "ipv6")
        self.assertGreater(time.monotonic() - started, 8, "longer than 3.1 waited")
        self.assertEqual(self.through(client.port, "GET", "/ask?challenge=1")[0], 200)

    def test_with_ipv6_that_lets_nothing_in_the_message_says_what_would_help(self):
        self.starlink_host(v6=natsim.V6_BLOCKS_INBOUND)
        time.sleep(2.0)
        with self.assertRaises(PunchError) as caught:
            self.starlink_join(v6=natsim.V6_BLOCKS_INBOUND, connect_seconds=4)
        text = str(caught.exception)
        self.assertIn("did not answer within 4 seconds", text)
        self.assertIn("strict carrier-grade NAT", text)
        self.assertIn("relay/README.md", text)
        self.assertNotIn("TimeoutError", text)
        facts = caught.exception.facts
        self.assertEqual((facts["host_found"], facts["host_v6"], facts["host_nat"], facts["my_nat"]),
                         (True, True, "hard", "hard"))


class LoggingTests(RendezvousBase):
    """Both sides log every step at INFO: enough to tell from the log alone what was tried and where it stopped."""

    def test_the_join_is_in_the_log_on_both_sides_and_no_secret_is(self):
        with self.assertLogs("privatepartynet", level="INFO") as logs:
            host, _, _ = self.starlink_host()
            time.sleep(2.0)
            dead = socket.socket()
            dead.bind(("127.0.0.1", 0))
            dead_port = dead.getsockname()[1]
            dead.close()
            invite = connect.decode_invite(connect.encode_invite(
                "Heroes night", PASSWORD, [["lan", "127.0.0.1", dead_port]], self.fingerprint,
                server_id="feedfacecafebeef", host_sign=self.host_identity.sign_public.hex(),
                secret=self.secret.hex()))
            v4, v6 = self.internet.router(natsim.SYMMETRIC), self.internet.router(natsim.V6_STATEFUL)
            original = punch.join_through

            def join(*args, **kwargs):
                with v4.adopting(), v6.adopting(), mock.patch.object(punch, "_v6_socket", dual_stack_socket):
                    return original(*args, **{**kwargs, "dht_factory": self.dht, "stun_servers": self.stun,
                                              "connect_seconds": 30, "find_seconds": 10,
                                              "ipv6": lambda: [v6.mapped_address]})

            with mock.patch.object(punch, "join_through", join):
                joiner = connect.Joiner(invite, "Far Away", identity=Identity.create(), my_lan=[], poll=0.1,
                                        timeout=3.0)
                thread = threading.Thread(target=joiner.run, daemon=True)
                thread.start()
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline and joiner.status["state"] != "waiting":
                    time.sleep(0.1)
            self.assertEqual(joiner.status["state"], "waiting", joiner.status)
            joiner.cancel()
            thread.join(10)
            self.assertIsNone(joiner.tunnel, "a cancelled join closes its tunnel")
        text = "\n".join(logs.output)
        for expected in ("Joining Heroes night: the invite was made by PrivatePartyNet 3.",  # the invite's contents
                         "addresses: 1 lan; IPv6: no; relays: 0 relay server, 0 members who relay",
                         f"Trying lan address 127.0.0.1 port {dead_port}: no answer",  # each address and its result
                         "Found the host on the DHT",  # the DHT lookup
                         "router gives a new port for every destination",  # the NAT type found
                         "Not trying IPv4: both routers give a new port",  # why a rung was skipped
                         "Wrote this PC's addresses to the invite's mailbox",
                         "Reached the host over IPv6",  # the rung that worked
                         # the host's side: what it offers, the joiner it noticed, the tunnel and the request
                         "Ready to be reached through the routers. IPv4:", "IPv6: ::ffff:127.77.",
                         "A joiner wrote to the mailbox of invite", "A joiner's tunnel to the party opened, over IPv6",
                         "A request to join through the routers or a relay (a tunnel) (Far Away): taken"):
            self.assertIn(expected, text)
        for secret in (PASSWORD, self.secret.hex(), self.host_identity.sign_public.hex()):
            self.assertNotIn(secret, text, "never the password or keys")


class IdleHostTests(RendezvousBase):
    """The user's real case in 3.5.0: two PCs behind ordinary home routers (one port per socket), each hosting for half an
    hour before the other tried to join. Both found the other on the DHT, and nothing came back either way. The host
    learned its public port once, at the start, and sent nothing from that socket after; a router forgets an idle
    mapping (often within a few minutes, some within 30 seconds), so the port on the DHT led nowhere. The host now asks
    STUN again every KEEPALIVE_SECONDS from the same socket, which keeps the mapping, and publishes a new port at once
    if the router gave one anyway."""

    def home_host(self, mapping_seconds, refresh=1.0):
        v4 = self.internet.router(natsim.PORT_RESTRICTED, mapping_seconds=mapping_seconds)
        with v4.adopting():
            host = HostPunch(self.host_identity._sign, "feedfacecafebeef", self.certfile, self.keyfile,
                             self.party.port, lambda: [self.secret], dht_factory=self.dht, stun_servers=self.stun,
                             refresh=refresh, poll=0.5, ipv6=lambda: [])
        self.addCleanup(host.close)
        return host, v4

    def home_join(self):
        v4 = self.internet.router(natsim.PORT_RESTRICTED)
        with v4.adopting():
            client = join_through(self.host_identity.sign_public.hex(), "feedfacecafebeef", self.secret,
                                  self.fingerprint, dht_factory=self.dht, stun_servers=self.stun, connect_seconds=20,
                                  find_seconds=10, ipv6=lambda: [])
        self.addCleanup(client.close)
        return client

    def test_a_host_idle_for_longer_than_its_router_remembers_is_still_reached(self):
        with mock.patch.object(punch, "KEEPALIVE_SECONDS", 0.5, create=True):
            host, _ = self.home_host(mapping_seconds=1.5)
            first = host.mapping
            time.sleep(4.0)  # idle well past the router's memory of the mapping
            client = self.home_join()
            self.assertEqual(self.through(client.port, "GET", "/ask?challenge=1")[0], 200)
            self.assertEqual(host.mapping, first, "the mapping was kept, so its port never changed")

    def test_a_host_whose_router_gave_it_a_new_port_publishes_the_new_one(self):
        with mock.patch.object(punch, "KEEPALIVE_SECONDS", 0.5, create=True):
            host, v4 = self.home_host(mapping_seconds=None, refresh=60.0)  # no regular republish within the test
            first = host.mapping
            time.sleep(1.5)  # the first record is published
            with self.internet._lock:
                for mapping in v4._mappings.values():
                    mapping.dead = True  # the router dropped it (rebooted, or a timer shorter than the keepalive)
            self.assertTrue(wait_until(lambda: host.mapping[:2] != first[:2], 5), "the host noticed its new port")
            client = self.home_join()
            self.assertEqual(self.through(client.port, "GET", "/ask?challenge=1")[0], 200)


def wait_until(check, seconds):
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        if check():
            return True
        time.sleep(0.1)
    return check()


class MessageTests(unittest.TestCase):
    """The final message: what was tried, what was missing, in words. Never "(TimeoutError)"."""

    def users_invite(self, **extra):
        return {"name": "Party", "password": "x", "fingerprint": "ab" * 32, "secret": "cd" * 16,
                "host_sign": "ef" * 32, "server_id": "feedfacecafebeef", "relays": [], "app": "",
                "candidates": [["lan", "10.214.67.2", 8765], ["lan", "192.168.0.12", 8765]], **extra}

    def test_the_users_real_case_says_what_was_tried_and_what_was_missing(self):
        tried = [("lan", "10.214.67.2", "10.214.67.2 did not answer"), ("lan", "192.168.0.12", "192.168.0.12 did not answer")]
        error = PunchError("the host's PC did not answer within 100 seconds: it never noticed this request, or its "
                           "routers let nothing through", {"host_found": True, "host_version": "", "host_v6": False,
                                                           "host_nat": "easy", "my_v6": True, "my_nat": "hard"})
        text = connect.failure_text(self.users_invite(), tried, error)
        for expected in ("10.214.67.2 did not answer; 192.168.0.12 did not answer",
                         "Those are addresses on the host's own network",
                         "The invite has no IPv6 address",
                         "Connecting through both routers did not work: the host's PC did not answer within 100 seconds",
                         "No relay server is set up for this party",
                         "The host is running an older PrivatePartyNet (3.1 or earlier)"):
            self.assertIn(expected, text)
        self.assertNotRegex(text, r"\(\w*(Error|Exception)\)", "no exception name")

    def test_two_strict_routers_point_at_a_relay(self):
        error = PunchError(punch.BOTH_HARD, {"host_found": True, "host_version": "3.2.0", "host_v6": True,
                                             "my_v6": False})
        text = connect.failure_text(self.users_invite(app="3.2.0"), [], error)
        self.assertIn("both routers give a new port for every connection (strict carrier-grade NAT), so a direct way "
                      "cannot open. Set up a relay server (relay/README.md) or have a member turn on friend relay", text)
        self.assertIn("The host has IPv6 but this PC has none", text)
        self.assertNotIn("older PrivatePartyNet", text, "a 3.2 host is not old")

    def test_a_host_not_found_on_the_dht_says_so(self):
        error = PunchError("the host's PC was not found on the DHT (the shared lookup network the invite uses) within "
                           "60 seconds: their party may be closed, or their PC cannot reach the DHT", {"host_found": False})
        text = connect.failure_text(self.users_invite(app="3.2.0", candidates=[["ipv6", "2001:db8::5", 8765]]),
                                    [("ipv6", "2001:db8::5", "2001:db8::5 did not answer")], error)
        self.assertIn("was not found on the DHT", text)
        self.assertNotIn("no IPv6 address", text)
        self.assertNotIn("older", text)


    def test_a_nearby_or_address_join_is_not_told_about_invites(self):
        nearby = {"name": "Host", "password": "", "fingerprint": "ab" * 32, "server_id": "", "host_sign": "",
                  "candidates": [["lan", "26.10.20.30", 8765]], "source": "nearby"}
        text = connect.failure_text(nearby, [("lan", "26.10.20.30", "26.10.20.30 did not answer")], None,
                                    "the invite has no key for finding the host across the internet")
        self.assertIn("Tried: 26.10.20.30 did not answer.", text)
        self.assertNotIn("invite", text)
        self.assertIn("server is running", text)


class OldInviteTests(unittest.TestCase):
    def test_a_3_0_invite_is_read_and_known_for_an_old_one(self):
        code = invite_of_3_0([["lan", "192.168.0.12", 8765]], "ab" * 32, "cd" * 16, "ef" * 32)
        invite = connect.decode_invite(code)
        self.assertEqual((invite["app"], invite["relays"]), ("", []))
        self.assertIn("made by PrivatePartyNet 3.1 or older", connect.describe_invite(invite))
        text = connect.failure_text({**invite, "secret": ""}, [("lan", "192.168.0.12", "192.168.0.12 did not answer")],
                                    None, "the invite has no key for finding the host across the internet")
        self.assertIn("The invite comes from an older PrivatePartyNet (3.1 or earlier)", text)
        self.assertIn("Both PCs need 3.1 or later for IPv6 and relays", text)

    def test_a_new_invite_says_its_version_and_an_old_copy_still_reads_it(self):
        from privatepartynet import __version__

        code = connect.encode_invite("P", "x", [["lan", "192.168.0.12", 8765]], "ab" * 32)
        self.assertEqual(connect.decode_invite(code)["app"], __version__)
        raw = base64.urlsafe_b64decode(code[5:] + "=" * (-len(code[5:]) % 4))
        self.assertEqual(json.loads(zlib.decompress(raw[:-3]))["v"], 1, "same invite format: 3.1 reads it as before")


class ControllerTests(unittest.TestCase):
    """The app's side: what an invite carries from the start, a host seen nearby, and a nearby party's password."""

    def setUp(self):
        import tempfile

        from privatepartynet.app import Controller
        from test_app import FakeGrabber, free_port

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.host = Controller(data_folder=folder.name + "/host", discovery_port=0, grabber=FakeGrabber())
        self.guest = Controller(data_folder=folder.name + "/guest", discovery_port=0, grabber=FakeGrabber())
        self.addCleanup(self.host.shutdown)
        self.addCleanup(self.guest.shutdown)
        self.assertTrue(self.host.start("Host", free_port())["ok"])
        self.guest._name = "Guest"

    def nearby(self, controller, peer):
        controller._discovery = type("FakeDiscovery", (), {"peer_list": lambda self_: [peer], "stop": lambda self_: None,
                                                           "set_identity": lambda *a: None, "request": lambda *a: False})()

    def host_peer(self):
        return {"id": "inst", "name": "Host", "address": "127.0.0.1", "port": self.host._party.port, "sharing": True,
                "mode": "party", "user": self.host._identity.user_id, "sign": self.host._identity.sign_public.hex(),
                "dh": self.host._identity.dh_public.hex(), "dm": None, "fp": self.host._party.fingerprint}

    def test_an_invite_made_before_the_router_check_carries_ipv6(self):
        self.assertIsNone(self.host._reach, "no router check in a test, as in the first seconds of a real party")
        with mock.patch.object(connect, "global_ipv6_addresses", return_value=["2001:db8::5"]):
            invite = connect.decode_invite(self.host.invite()["invite"])
        self.assertIn(["ipv6", "2001:db8::5", self.host._party.port], invite["candidates"])

    def test_a_host_also_seen_nearby_is_tried_there_first(self):
        invite = connect.decode_invite(self.host.invite()["invite"])
        invite["candidates"] = [["lan", "192.0.2.1", 9]]  # an address from the host's own network, dead from here
        self.nearby(self.guest, self.host_peer())
        with self.assertLogs("privatepartynet", level="INFO") as logs:
            self.assertTrue(self.guest._begin_join(invite)["ok"])
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and self.guest.join_status()["state"] != "waiting":
                time.sleep(0.1)
        self.assertEqual(self.guest.join_status()["state"], "waiting")
        self.assertIn("The invite's host is also nearby, at 127.0.0.1", "\n".join(logs.output))
        self.guest.join_cancel()

    def test_a_nearby_party_that_wants_its_password_asks_for_it(self):
        self.host._party.trust_local = False  # as over Radmin VPN: this PC counts as outside the host's network
        self.nearby(self.guest, self.host_peer())
        self.assertTrue(self.guest.ask_join("inst")["ok"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.guest.join_status()["state"] in ("trying", "waiting"):
            time.sleep(0.1)
        status = self.guest.join_status()
        self.assertEqual((status["state"], status["needs_password"], status["peer"]), ("failed", True, "inst"))
        self.assertIn("Type it below", status["message"])
        self.assertTrue(self.guest.ask_join("inst", self.host._passcode)["ok"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.guest.join_status()["state"] != "waiting":
            time.sleep(0.1)
        self.assertEqual(self.guest.join_status()["state"], "waiting", "the password got it through the door")
        self.assertFalse(self.guest.join_status()["needs_password"])
        self.guest.join_cancel()

    def test_the_page_offers_a_password_box_for_a_nearby_party(self):
        from privatepartynet.paths import web_dir

        page = (web_dir() / "control.html").read_text(encoding="utf-8")
        script = (web_dir() / "control.js").read_text(encoding="utf-8")
        self.assertIn('id="askPassword"', page)
        self.assertIn("ask_join(peer, el.askPassword.value)", script)
        self.assertIn("state.join.needs_password", script)
        style = (web_dir() / "control.css").read_text(encoding="utf-8")
        # .row sets display: flex, which beats the hidden attribute; the 3.2.0 build first showed the box at all times
        self.assertIn(".row[hidden] { display: none; }", style)


class VpnAdapterTests(unittest.TestCase):
    ADAPTERS = [("Wi-Fi", "192.168.0.12"), ("Radmin VPN", "26.10.20.30"), ("PrivatePartyNet", "10.214.67.2"),
                ("Ethernet 3", "169.254.10.20"), ("Tailscale", "100.101.102.103")]

    def test_every_adapter_is_listed_but_the_game_networks_own(self):
        with mock.patch("socket.gethostbyname_ex", return_value=("pc", [], ["192.168.0.12", "10.214.67.2"])):
            found = lan_addresses(self.ADAPTERS)
        self.assertIn("26.10.20.30", found, "Radmin VPN, which the host name's addresses can leave out")
        self.assertIn("100.101.102.103", found)
        self.assertNotIn("10.214.67.2", found, "the game network's adapter is reachable only through itself")
        self.assertEqual(found[-1], "169.254.10.20", "a self-assigned address last")

    def test_the_invite_tries_an_address_on_a_vpn_both_pcs_are_on_first(self):
        order = connect._order([["lan", "192.168.0.12", 1], ["ipv6", "2001:db8::5", 1], ["lan", "26.10.20.30", 1]],
                               my_lan=["192.168.1.40", "26.1.2.3"])
        self.assertEqual(order[0][1], "26.10.20.30")
        self.assertEqual(connect.vpn_name("26.10.20.30"), "Radmin VPN")
        self.assertEqual(connect.vpn_name("25.1.2.3"), "Hamachi")
        self.assertEqual(connect.vpn_name("192.168.0.12"), "")


if __name__ == "__main__":
    unittest.main()
