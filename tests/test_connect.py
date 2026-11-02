import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from privatepartynet import connect
from privatepartynet.cert import ensure_certificate
from privatepartynet.connect import (ConnectError, Joiner, UpnpMapping, certificate_fingerprint, classify_public_ip, decode_invite,
                              encode_invite, gather, stun_probe)
from privatepartynet.server import Party

MAGIC = 0x2112A442


class FakeStun:
    """A STUN server on loopback. `report(source)` decides which (ip, port) it says it saw, so a test can make a
    router that keeps one mapping (always the same port) or one that picks a new port per server."""

    def __init__(self, report=None, silent=False):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.2)
        self.report = report or (lambda source: source)
        self.silent = silent
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    @property
    def address(self):
        return ("127.0.0.1", self.socket.getsockname()[1])

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, source = self.socket.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            if self.silent or len(data) < 20:
                continue
            ip, port = self.report(source)
            xor_port = port ^ (MAGIC >> 16)
            xor_ip = struct.unpack("!I", socket.inet_aton(ip))[0] ^ MAGIC
            attribute = struct.pack("!HHBBH", 0x0020, 8, 0, 1, xor_port) + struct.pack("!I", xor_ip)
            self.socket.sendto(struct.pack("!HHI", 0x0101, len(attribute), MAGIC) + data[8:20] + attribute, source)

    def close(self):
        self._stop.set()
        self.thread.join(timeout=2)
        self.socket.close()


class StunTests(unittest.TestCase):
    def servers(self, *reports, silent=0):
        fakes = [FakeStun(report) for report in reports] + [FakeStun(silent=True) for _ in range(silent)]
        for fake in fakes:
            self.addCleanup(fake.close)
        return tuple(fake.address for fake in fakes)

    def test_one_port_from_every_server_is_an_easy_router(self):
        same = lambda source: ("203.0.113.7", 40000)
        result = stun_probe(self.servers(same, same, same), timeout=1.0)
        self.assertEqual((result["public"], result["port"], result["nat"], result["answers"]), ("203.0.113.7", 40000, "easy", 3))

    def test_a_different_port_from_each_server_is_a_hard_router(self):
        ports = iter(range(41000, 41010))
        result = stun_probe(self.servers(*[lambda source, ports=ports: ("203.0.113.7", next(ports))] * 3), timeout=1.0)
        self.assertEqual(result["nat"], "hard")

    def test_one_answer_is_not_enough_to_tell(self):
        result = stun_probe(self.servers(lambda source: ("203.0.113.7", 40000), silent=2), timeout=0.3)
        self.assertEqual((result["nat"], result["answers"], result["public"]), ("unknown", 1, "203.0.113.7"))

    def test_no_answers_is_no_address(self):
        result = stun_probe(self.servers(silent=2), timeout=0.3)
        self.assertEqual((result["public"], result["nat"], result["answers"]), (None, "unknown", 0))

    def test_an_unreachable_server_name_does_not_stop_the_others(self):
        good = self.servers(lambda source: ("203.0.113.7", 40000), lambda source: ("203.0.113.7", 40000))
        result = stun_probe((("no-such-host.invalid", 3478),) + good, timeout=0.5)
        self.assertEqual(result["answers"], 2)


class AddressTests(unittest.TestCase):
    def test_carrier_grade_nat_addresses_are_recognised(self):
        self.assertEqual(classify_public_ip("100.64.12.3"), "shared")
        self.assertEqual(classify_public_ip("100.127.255.255"), "shared")
        self.assertEqual(classify_public_ip("100.128.0.1"), "public")
        self.assertEqual(classify_public_ip("192.168.0.5"), "private")
        self.assertEqual(classify_public_ip("10.1.2.3"), "private")
        self.assertEqual(classify_public_ip("9.9.9.9"), "public")
        self.assertEqual(classify_public_ip("not an address"), "unknown")

    def test_only_native_global_ipv6_addresses_count(self):
        import ipaddress
        from unittest import mock

        def fake(host, port, family):
            return [(family, 0, 0, "", (a, 0, 0, 0)) for a in
                    ("fe80::1%12", "fd00::5", "::1", "2001:0:14c9:dc04::1", "2002:c000:204::1", "2603:6011:abcd::10", "2603:6011:abcd::10")]

        with mock.patch.object(connect.socket, "getaddrinfo", fake):
            self.assertEqual(connect.global_ipv6_addresses(), ["2603:6011:abcd::10"])


class GatherTests(unittest.TestCase):
    OK = {"ok": True, "external_ip": "9.9.9.9", "external_port": 8765, "reason": ""}
    NO = {"ok": False, "external_ip": None, "external_port": None, "reason": "no router answered"}

    def test_an_open_router_on_a_public_address_is_reachable(self):
        report = gather(8765, upnp=self.OK, stun={"public": "9.9.9.9", "nat": "easy"}, ipv6=[], lan=["192.168.0.5"])
        self.assertEqual(report["reachable"], "yes")
        self.assertIn(["public", "9.9.9.9", 8765], report["candidates"])
        self.assertIn(["lan", "192.168.0.5", 8765], report["candidates"])
        self.assertFalse(report["cgnat"])

    def test_a_shared_address_is_reported_as_unreachable_with_the_reason(self):
        report = gather(8765, upnp=self.NO, stun={"public": "100.66.1.2", "nat": "easy"}, ipv6=[], lan=["192.168.1.9"])
        self.assertTrue(report["cgnat"])
        self.assertEqual(report["reachable"], "no")
        self.assertIn("carrier-grade", report["advice"])
        self.assertEqual([c[0] for c in report["candidates"]], ["lan"])

    def test_a_router_whose_outside_address_is_not_the_internets_means_a_second_nat(self):
        upnp = {"ok": True, "external_ip": "10.5.5.5", "external_port": 8765, "reason": ""}
        report = gather(8765, upnp=upnp, stun={"public": "9.9.9.9", "nat": "easy"}, ipv6=[], lan=["192.168.1.9"])
        self.assertTrue(report["cgnat"])
        self.assertNotIn("public", [c[0] for c in report["candidates"]], "a private address is no use to outsiders")

    def test_ipv6_makes_a_pc_reachable_even_behind_a_shared_address(self):
        report = gather(8765, upnp=self.NO, stun={"public": "100.66.1.2", "nat": "hard"}, ipv6=["2603:6011:abcd::10"], lan=["192.168.1.9"])
        self.assertEqual(report["reachable"], "yes")
        self.assertIn(["ipv6", "2603:6011:abcd::10", 8765], report["candidates"])
        self.assertIn("bypass", report["advice"])

    def test_an_easy_router_that_did_not_open_the_port_is_a_maybe(self):
        report = gather(8765, upnp=self.NO, stun={"public": "9.9.9.9", "nat": "easy"}, ipv6=[], lan=["192.168.0.5"])
        self.assertEqual(report["reachable"], "maybe")

    def test_no_internet_is_said_plainly(self):
        report = gather(8765, upnp=self.NO, stun={"public": None, "nat": "unknown"}, ipv6=[], lan=["192.168.0.5"])
        self.assertEqual(report["reachable"], "maybe")
        self.assertIn("internet connection", report["advice"])


# --- a router that speaks UPnP ------------------------------------------------------------------------------------

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceList><device><deviceList><device><serviceList>
<service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType><controlURL>/ctl/IPConn</controlURL></service>
</serviceList></device></deviceList></device></deviceList></device></root>"""


class FakeRouter:
    def __init__(self, accept_timed_leases=True, refuse=False, external="9.9.9.9"):
        self.mappings = {}
        self.calls = []
        self.accept_timed_leases = accept_timed_leases
        self.refuse = refuse
        self.external = external
        router = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = DESCRIPTION.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                action = self.headers["SOAPAction"].strip('"').split("#")[1]
                router.calls.append(action)
                status, reply = 200, "<ok/>"
                if action == "AddPortMapping":
                    lease = int(body.split("<NewLeaseDuration>")[1].split("<")[0])
                    port = int(body.split("<NewExternalPort>")[1].split("<")[0])
                    if router.refuse or (lease and not router.accept_timed_leases):
                        status = 500
                    else:
                        router.mappings[port] = lease
                elif action == "DeletePortMapping":
                    router.mappings.pop(int(body.split("<NewExternalPort>")[1].split("<")[0]), None)
                elif action == "GetExternalIPAddress":
                    reply = f"<NewExternalIPAddress>{router.external}</NewExternalIPAddress>"
                data = reply.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.ssdp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ssdp.bind(("127.0.0.1", 0))
        self.ssdp.settimeout(0.2)
        self._stop = threading.Event()
        self.ssdp_thread = threading.Thread(target=self._ssdp, daemon=True)
        self.ssdp_thread.start()

    @property
    def search_address(self):
        return ("127.0.0.1", self.ssdp.getsockname()[1])

    def _ssdp(self):
        while not self._stop.is_set():
            try:
                data, source = self.ssdp.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            if b"M-SEARCH" in data:
                reply = f"HTTP/1.1 200 OK\r\nLOCATION: http://127.0.0.1:{self.http.server_address[1]}/desc.xml\r\nST: x\r\n\r\n"
                self.ssdp.sendto(reply.encode(), source)

    def close(self):
        self._stop.set()
        self.http.shutdown()
        self.http.server_close()
        self.ssdp_thread.join(timeout=2)
        self.ssdp.close()


class UpnpTests(unittest.TestCase):
    def router(self, **options):
        router = FakeRouter(**options)
        self.addCleanup(router.close)
        return router

    def mapping(self, router, port=8765):
        mapping = UpnpMapping(port, search_address=router.search_address, timeout=1.0)
        self.addCleanup(mapping.unmap)
        return mapping

    def test_the_port_is_opened_and_the_outside_address_is_read(self):
        router = self.router()
        result = self.mapping(router).map()
        self.assertEqual((result["ok"], result["external_ip"], result["external_port"]), (True, "9.9.9.9", 8765))
        self.assertIn(8765, router.mappings)

    def test_the_router_address_is_known_after_it_answers(self):
        router = self.router()
        mapping = self.mapping(router)
        self.assertIsNone(mapping.gateway_address)
        mapping.map()
        self.assertEqual(mapping.gateway_address, "127.0.0.1")

    def test_unmapping_takes_the_request_back(self):
        router = self.router()
        mapping = self.mapping(router)
        mapping.map()
        mapping.unmap()
        self.assertEqual(router.mappings, {})
        self.assertIn("DeletePortMapping", router.calls)

    def test_a_router_that_refuses_timed_leases_gets_an_open_ended_one(self):
        router = self.router(accept_timed_leases=False)
        self.assertTrue(self.mapping(router).map()["ok"])
        self.assertEqual(router.mappings[8765], 0)

    def test_a_router_that_refuses_is_reported(self):
        router = self.router(refuse=True)
        result = self.mapping(router).map()
        self.assertFalse(result["ok"])
        self.assertIn("refused", result["reason"])

    def test_no_router_answering_is_reported_not_raised(self):
        silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        silent.bind(("127.0.0.1", 0))
        self.addCleanup(silent.close)
        result = UpnpMapping(8765, search_address=("127.0.0.1", silent.getsockname()[1]), timeout=0.4).map()
        self.assertFalse(result["ok"])
        self.assertIn("did not answer", result["reason"])

    def test_gather_uses_the_mapping(self):
        router = self.router()
        mapping = self.mapping(router)
        report = gather(8765, upnp=mapping.map(), stun={"public": "9.9.9.9", "nat": "easy"}, ipv6=[], lan=["192.168.0.5"])
        self.assertEqual(report["reachable"], "yes")


# --- invites ----------------------------------------------------------------------------------------------------

FINGERPRINT = "ab" * 32


class InviteTests(unittest.TestCase):
    def test_an_invite_round_trips(self):
        code = encode_invite("Heroes night", "428617", [["lan", "192.168.0.5", 8765], ["ipv6", "2603::1", 8765]], FINGERPRINT,
                             server_id="s1", host_sign="cd" * 32)
        self.assertTrue(code.startswith("ppn1-"))
        got = decode_invite(code)
        self.assertEqual((got["name"], got["password"], got["fingerprint"], got["server_id"]), ("Heroes night", "428617", FINGERPRINT, "s1"))
        self.assertEqual(got["candidates"], [["lan", "192.168.0.5", 8765], ["ipv6", "2603::1", 8765]])

    def test_spaces_and_line_breaks_from_pasting_are_ignored(self):
        code = encode_invite("A", "1", [["lan", "10.0.0.2", 8765]], FINGERPRINT)
        pasted = " " + code[:20] + "\n" + code[20:] + "  "
        self.assertEqual(decode_invite(pasted)["name"], "A")

    def test_a_typo_is_caught(self):
        code = encode_invite("A", "1", [["lan", "10.0.0.2", 8765]], FINGERPRINT)
        broken = code[:-6] + ("A" if code[-6] != "A" else "B") + code[-5:]
        with self.assertRaisesRegex(ConnectError, "damaged"):
            decode_invite(broken)

    def test_things_that_are_not_invites_are_refused_in_words(self):
        for text in ("", "hello", "ppn1-", "ppn1-!!!!", "https://example.com"):
            with self.assertRaises(ConnectError):
                decode_invite(text)

    def test_nonsense_addresses_and_ports_are_dropped(self):
        code = encode_invite("A", "1", [["lan", "not-an-ip", 8765], ["lan", "10.0.0.2", 99999], ["weird", "10.0.0.2", 80],
                                        ["public", "9.9.9.9", 8765]], FINGERPRINT)
        self.assertEqual(decode_invite(code)["candidates"], [["public", "9.9.9.9", 8765]])

    def test_an_invite_without_a_fingerprint_is_refused(self):
        code = encode_invite("A", "1", [["lan", "10.0.0.2", 8765]], "short")
        with self.assertRaisesRegex(ConnectError, "certificate"):
            decode_invite(code)

    def test_an_oversized_payload_cannot_inflate(self):
        import base64
        import zlib
        bomb = zlib.compress(b"A" * 5_000_000)
        raw = bomb + __import__("hashlib").sha256(bomb).digest()[:3]
        with self.assertRaises(ConnectError):
            decode_invite("ppn1-" + base64.urlsafe_b64encode(raw).decode().rstrip("="))

    def test_the_fingerprint_is_the_hash_of_the_certificate(self):
        import hashlib
        import ssl
        with tempfile.TemporaryDirectory() as folder:
            certfile, _ = ensure_certificate(folder, ["127.0.0.1"])
            pem = Path(certfile).read_text(encoding="utf-8")
            self.assertEqual(certificate_fingerprint(pem), hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest())


# --- joining a real party over HTTPS ---------------------------------------------------------------------------

class JoinTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        certfile, keyfile = ensure_certificate(temp.name, ["localhost", "127.0.0.1"])
        self.party = Party("428617", 0, certfile, keyfile, party_name="Heroes night", host_name="Host",
                           failed_login_delay=0, heartbeat=0.2, trust_local=False)
        self.party.start()
        self.addCleanup(self.party.stop)
        self.fingerprint = certificate_fingerprint(Path(certfile).read_text(encoding="utf-8"))

    def invite(self, password="428617", fingerprint=None, port=None, address="127.0.0.1"):
        return decode_invite(encode_invite("Heroes night", password, [["lan", address, port or self.party.port]],
                                           fingerprint or self.fingerprint))

    def joiner(self, invite, **options):
        options.setdefault("my_lan", ["127.0.0.1"])
        options.setdefault("poll", 0.1)
        options.setdefault("timeout", 3.0)
        return Joiner(invite, "Newcomer", **options)

    def run_in_thread(self, joiner):
        thread = threading.Thread(target=joiner.run, daemon=True)
        thread.start()
        self.addCleanup(joiner.cancel)
        return thread

    def wait_for(self, joiner, state, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and joiner.status["state"] != state:
            time.sleep(0.05)
        return joiner.status["state"] == state

    def test_asking_waits_then_walks_in_when_the_owner_says_yes(self):
        joiner = self.joiner(self.invite())
        thread = self.run_in_thread(joiner)
        self.assertTrue(self.wait_for(joiner, "waiting"))
        (request,) = self.party.pending_requests()
        self.assertEqual(request["name"], "Newcomer")
        self.party.decide_request(self.party.host_id, request["id"], True)
        self.assertTrue(self.wait_for(joiner, "allowed"))
        thread.join(timeout=3)
        url = joiner.status["url"]
        self.assertTrue(url.startswith(f"https://127.0.0.1:{self.party.port}/join?token="))
        token = url.split("token=")[1]
        self.assertIsNotNone(self.party.redeem(token))

    def test_a_no_is_passed_on(self):
        joiner = self.joiner(self.invite())
        self.run_in_thread(joiner)
        self.assertTrue(self.wait_for(joiner, "waiting"))
        (request,) = self.party.pending_requests()
        self.party.decide_request(self.party.host_id, request["id"], False)
        self.assertTrue(self.wait_for(joiner, "denied"))

    def test_a_wrong_password_is_reported_as_that(self):
        joiner = self.joiner(self.invite(password="000000"))
        self.run_in_thread(joiner)
        self.assertTrue(self.wait_for(joiner, "failed"))
        self.assertIn("password", joiner.status["message"])
        self.assertEqual(self.party.pending_requests(), [])

    def test_a_different_certificate_is_refused_before_anything_is_sent(self):
        joiner = self.joiner(self.invite(fingerprint="00" * 32))
        self.run_in_thread(joiner)
        self.assertTrue(self.wait_for(joiner, "failed"))
        self.assertIn("certificate", joiner.status["message"])
        self.assertEqual(self.party.pending_requests(), [])

    def test_a_dead_address_falls_through_to_the_next_and_says_why_when_none_work(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        invite = decode_invite(encode_invite("P", "428617", [["lan", "127.0.0.1", dead_port], ["public", "127.0.0.1", self.party.port]],
                                             self.fingerprint))
        joiner = self.joiner(invite)
        self.run_in_thread(joiner)
        self.assertTrue(self.wait_for(joiner, "waiting"), "the second address worked")
        joiner.cancel()
        nowhere = decode_invite(encode_invite("P", "1", [["lan", "127.0.0.1", dead_port]], self.fingerprint))
        failing = self.joiner(nowhere)
        failing.run()
        self.assertEqual(failing.status["state"], "failed")
        self.assertIn("127.0.0.1 did not answer", failing.status["message"], "it says what it tried")

    def test_waiting_gives_up_after_its_limit(self):
        joiner = self.joiner(self.invite(), answer_wait=0.5)
        joiner.run()
        self.assertEqual(joiner.status["state"], "failed")
        self.assertIn("Nobody answered", joiner.status["message"])

    def test_cancelling_stops_the_wait(self):
        joiner = self.joiner(self.invite())
        thread = self.run_in_thread(joiner)
        self.assertTrue(self.wait_for(joiner, "waiting"))
        joiner.cancel()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(joiner.status["state"], "failed")

    def test_a_signed_joiner_is_known_the_second_time_and_comes_straight_in(self):
        from privatepartynet.identity import Identity

        me = Identity.create()
        first = self.joiner(self.invite(), identity=me)
        self.run_in_thread(first)
        self.assertTrue(self.wait_for(first, "waiting"))
        (request,) = self.party.pending_requests()
        self.assertEqual(request["user"], me.user_id)
        self.party.decide_request(self.party.host_id, request["id"], True)
        self.assertTrue(self.wait_for(first, "allowed"))
        self.party.redeem(first.status["url"].split("token=")[1])
        again = self.joiner(self.invite(password="not even the password"), identity=me)
        again.run()
        self.assertEqual(again.status["state"], "allowed", "a returning member needs neither password nor a yes")

    def test_joining_by_address_pins_the_first_certificate_it_sees(self):
        from privatepartynet.identity import Identity

        invite = {"name": "P", "password": "428617", "candidates": [["lan", "127.0.0.1", self.party.port]],
                  "fingerprint": "", "server_id": "", "host_sign": ""}
        joiner = self.joiner(invite, identity=Identity.create())
        self.run_in_thread(joiner)
        self.assertTrue(self.wait_for(joiner, "waiting"))
        self.assertEqual(invite["fingerprint"], self.fingerprint)

    def test_the_closest_addresses_are_tried_first(self):
        order = connect._order([["public", "9.9.9.9", 1], ["ipv6", "2603::1", 1], ["lan", "10.9.9.9", 1], ["lan", "192.168.0.5", 1]],
                               my_lan=["192.168.0.20"])
        self.assertEqual([c[1] for c in order], ["192.168.0.5", "2603::1", "9.9.9.9", "10.9.9.9"])


if __name__ == "__main__":
    unittest.main()
