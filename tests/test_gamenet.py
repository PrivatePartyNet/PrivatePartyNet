import ipaddress
import tempfile
import threading
import unittest
from pathlib import Path

from privatepartynet import gamenet
from privatepartynet.gamenet import (Directory, NetError, clean_candidates, is_group, make_packet, new_private_key,
                                     open_roster, parse, public_key, roster_body, sign_roster, subnet_for)
from privatepartynet.identity import Identity

NET = ipaddress.ip_network("10.77.12.0/24")


class AddressTests(unittest.TestCase):
    def test_the_same_server_always_gets_the_same_network(self):
        self.assertEqual(subnet_for("abc123"), subnet_for("abc123"))
        self.assertNotEqual(subnet_for("abc123"), subnet_for("def456"))

    def test_networks_are_slash_24s_in_ten_dot_and_avoid_common_ones(self):
        for number in range(300):
            network = subnet_for(f"server{number}")
            self.assertEqual(network.prefixlen, 24)
            self.assertTrue(network.subnet_of(ipaddress.ip_network("10.0.0.0/8")))
            self.assertNotIn(int(str(network).split(".")[1]), gamenet.AVOID_SECOND_OCTET)

    def test_a_network_this_pc_is_already_on_is_skipped(self):
        first = subnet_for("abc123")
        moved = subnet_for("abc123", in_use=[str(first)])
        self.assertNotEqual(first, moved)
        self.assertFalse(moved.overlaps(first))

    def test_this_pcs_networks_are_read_at_their_real_size(self):
        networks = gamenet.local_networks()
        self.assertTrue(networks, "at least loopback")
        self.assertIn(ipaddress.ip_network("127.0.0.0/8"), networks, "loopback is a /8, not a /24")

    def test_a_slash_16_this_pc_is_on_is_avoided_whole(self):
        for number in range(200):
            self.assertFalse(subnet_for(f"s{number}", in_use=["10.77.0.0/16"]).overlaps(ipaddress.ip_network("10.77.0.0/16")))

    def test_members_keep_their_address_and_the_host_is_dot_one(self):
        saved = []
        directory = Directory(NET, on_change=saved.append, host_user="u-host")
        self.assertEqual(str(directory.address_for("u-host")), "10.77.12.1")
        alex = directory.address_for("u-alex")
        sam = directory.address_for("u-sam")
        self.assertEqual((str(alex), str(sam)), ("10.77.12.2", "10.77.12.3"))
        self.assertEqual(directory.address_for("u-alex"), alex)
        again = Directory(NET, assignments=saved[-1])
        self.assertEqual(again.address_for("u-sam"), sam, "remembered across restarts")
        self.assertEqual(len(saved), 2, "only new assignments are saved")

    def test_a_full_network_says_so(self):
        directory = Directory(NET, assignments={f"u{n}": n for n in range(2, 255)})
        with self.assertRaises(NetError):
            directory.address_for("one more")


class PacketTests(unittest.TestCase):
    def test_a_packet_reads_back_its_addresses(self):
        packet = make_packet("10.77.12.2", "10.77.12.3", b"hello")
        self.assertEqual(parse(packet), (ipaddress.IPv4Address("10.77.12.2"), ipaddress.IPv4Address("10.77.12.3")))

    def test_the_checksum_is_correct(self):
        packet = make_packet("10.77.12.2", "10.77.12.3", b"x")
        total = sum((packet[i] << 8) + packet[i + 1] for i in range(0, 20, 2))
        while total >> 16:
            total = (total & 0xFFFF) + (total >> 16)
        self.assertEqual(total, 0xFFFF)

    def test_junk_and_ipv6_are_not_packets(self):
        self.assertIsNone(parse(b""))
        self.assertIsNone(parse(b"\x60" + bytes(39)))  # IPv6
        self.assertIsNone(parse(b"\x45" + bytes(10)))  # cut short
        bad_length = bytearray(make_packet("10.77.12.2", "10.77.12.3", b"abc"))
        bad_length[2:4] = (500).to_bytes(2, "big")
        self.assertIsNone(parse(bytes(bad_length)))

    def test_what_counts_as_everyone(self):
        self.assertTrue(is_group(ipaddress.IPv4Address("10.77.12.255"), NET))
        self.assertTrue(is_group(ipaddress.IPv4Address("255.255.255.255"), NET))
        self.assertTrue(is_group(ipaddress.IPv4Address("239.255.255.250"), NET))
        self.assertFalse(is_group(ipaddress.IPv4Address("10.77.12.9"), NET))


class KeyTests(unittest.TestCase):
    def test_a_key_pair_is_wireguards_kind(self):
        private = new_private_key()
        self.assertEqual(len(bytes.fromhex(private)), 32)
        self.assertEqual(len(bytes.fromhex(public_key(private))), 32)
        self.assertNotEqual(public_key(private), public_key(new_private_key()))

    def test_each_server_has_its_own_key_kept_sealed(self):
        from privatepartynet.vault import Vault

        with tempfile.TemporaryDirectory() as folder:
            vault = Vault(Path(folder) / "keys", use_dpapi=False)
            vault.unlock()
            first = gamenet.load_key(vault, Path(folder) / "identity", "own-server")
            self.assertEqual(gamenet.load_key(vault, Path(folder) / "identity", "own-server"), first, "kept")
            self.assertNotEqual(gamenet.load_key(vault, Path(folder) / "identity", "joined-abc"), first, "one per server")
            stored = b"".join(p.read_bytes() for p in (Path(folder) / "identity" / gamenet.KEY_FOLDER).iterdir())
            self.assertNotIn(first.encode("ascii"), stored, "never on disk in the clear")
            self.assertNotIn(bytes.fromhex(first), stored)


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Directory(NET, host_user="u-host")
        self.keys = {name: public_key(new_private_key()) for name in ("host", "alex", "sam")}

    def test_members_join_with_their_key_and_everyone_sees_the_same_list(self):
        self.assertEqual(str(self.directory.join("u-host", self.keys["host"], [["lan", "192.168.1.5", 4000]])), "10.77.12.1")
        self.assertEqual(str(self.directory.join("u-alex", self.keys["alex"])), "10.77.12.2")
        peers = self.directory.peers()
        self.assertEqual([(p["user"], p["address"], p["key"]) for p in peers],
                         [("u-host", "10.77.12.1", self.keys["host"]), ("u-alex", "10.77.12.2", self.keys["alex"])])
        self.assertEqual(peers[0]["candidates"], [["lan", "192.168.1.5", 4000]])

    def test_someone_elses_key_or_no_key_is_refused(self):
        self.directory.join("u-alex", self.keys["alex"])
        with self.assertRaises(NetError):
            self.directory.join("u-sam", self.keys["alex"])
        with self.assertRaises(NetError):
            self.directory.join("u-sam", "not a key")

    def test_every_change_wakes_the_links_that_wait(self):
        woke = []
        version = self.directory.version
        waiter = threading.Thread(target=lambda: woke.append(self.directory.wait(version, 5)))
        waiter.start()
        self.directory.join("u-alex", self.keys["alex"])
        waiter.join(timeout=5)
        self.assertEqual(woke, [version + 1])
        self.assertEqual(self.directory.wait(self.directory.version, 0.05), self.directory.version, "nothing new: timeout")
        self.assertTrue(self.directory.set_candidates("u-alex", [["upnp", "203.0.113.9", 5000]]))
        self.assertFalse(self.directory.set_candidates("u-alex", [["upnp", "203.0.113.9", 5000]]),
                         "the same again is no change")
        self.assertTrue(self.directory.leave("u-alex"))
        self.assertEqual(self.directory.peers(), [])

    def test_a_late_leave_from_an_old_link_does_not_remove_the_new_one(self):
        self.directory.join("u-alex", self.keys["alex"])
        newer = public_key(new_private_key())
        self.directory.join("u-alex", newer)
        self.assertFalse(self.directory.leave("u-alex", self.keys["alex"]))
        self.assertEqual([p["key"] for p in self.directory.peers()], [newer])

    def test_candidates_are_kept_only_when_they_are_small_and_plain(self):
        self.assertEqual(clean_candidates([["lan", "10.0.0.5", 4000], ["key", "ab" * 32, "cd" * 32]]),
                         [["lan", "10.0.0.5", 4000], ["key", "ab" * 32, "cd" * 32]])
        self.assertEqual(clean_candidates("lan"), [])
        self.assertEqual(clean_candidates([["lan", {"x": 1}, 4000], [], ["x"] * 9, ["vps", True, 1]]), [])
        self.assertEqual(clean_candidates([["lan", "x" * 250, 1]] * 24), [], "too much altogether")


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.host = Identity.create()
        directory = Directory(NET, host_user="u-host")
        directory.join("u-host", public_key(new_private_key()))
        directory.join("u-alex", public_key(new_private_key()), [["lan", "192.168.1.9", 4100]])
        self.body = roster_body("feedface", NET, directory.version, directory.peers())

    def test_a_signed_list_opens_with_the_hosts_key(self):
        roster = open_roster(sign_roster(self.body, self.host.sign), self.host.sign_public.hex(), "feedface", str(NET))
        self.assertEqual([p["address"] for p in roster["peers"]], ["10.77.12.1", "10.77.12.2"])
        self.assertEqual(roster["peers"][1]["candidates"], [["lan", "192.168.1.9", 4100]])

    def test_a_list_signed_by_anyone_else_or_not_at_all_is_refused_when_the_host_is_known(self):
        someone = Identity.create()
        for signed in (sign_roster(self.body, someone.sign), sign_roster(self.body, None)):
            with self.assertRaises(NetError):
                open_roster(signed, self.host.sign_public.hex())
        changed = sign_roster(self.body, self.host.sign)
        changed["roster"] = changed["roster"].replace("10.77.12.2", "10.77.12.3")
        with self.assertRaises(NetError):
            open_roster(changed, self.host.sign_public.hex())

    def test_a_list_for_another_server_or_network_or_with_a_stray_address_is_refused(self):
        signed = sign_roster(self.body, self.host.sign)
        with self.assertRaises(NetError):
            open_roster(signed, self.host.sign_public.hex(), server_id="another")
        with self.assertRaises(NetError):
            open_roster(signed, self.host.sign_public.hex(), network="10.77.13.0/24")
        stray = roster_body("feedface", NET, 1, [{"user": "u-x", "address": "8.8.8.8", "key": public_key(new_private_key()),
                                                  "candidates": []}])
        with self.assertRaises(NetError):
            open_roster(sign_roster(stray, self.host.sign), self.host.sign_public.hex())


if __name__ == "__main__":
    unittest.main()
