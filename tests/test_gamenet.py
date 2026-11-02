import io
import ipaddress
import queue
import threading
import time
import unittest

from privatepartynet import gamenet
from privatepartynet.gamenet import (MemoryTun, NetError, Router, bridge, carried, is_group, make_packet, parse, read_frame,
                              subnet_for, write_frame)

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
        router = Router(NET, on_change=saved.append)
        self.assertEqual(str(router.address_for(None)), "10.77.12.1")
        alex = router.address_for("u-alex")
        sam = router.address_for("u-sam")
        self.assertEqual((str(alex), str(sam)), ("10.77.12.2", "10.77.12.3"))
        self.assertEqual(router.address_for("u-alex"), alex)
        again = Router(NET, assignments=saved[-1])
        self.assertEqual(again.address_for("u-sam"), sam, "remembered across restarts")
        self.assertEqual(len(saved), 2, "only new assignments are saved")

    def test_a_full_network_says_so(self):
        router = Router(NET, assignments={f"u{n}": n for n in range(2, 255)})
        with self.assertRaises(NetError):
            router.address_for("one more")


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

    def test_only_game_network_traffic_leaves_the_pc(self):
        self.assertTrue(carried(make_packet("10.77.12.2", "10.77.12.3"), NET))
        self.assertTrue(carried(make_packet("10.77.12.2", "255.255.255.255"), NET))
        self.assertFalse(carried(make_packet("10.77.12.2", "8.8.8.8"), NET), "never a route to the internet")
        self.assertFalse(carried(make_packet("10.77.12.2", "192.168.0.5"), NET), "never a route into someone's home network")
        self.assertFalse(carried(b"\x60" + bytes(39), NET))


class FrameTests(unittest.TestCase):
    def test_frames_round_trip_with_keepalives(self):
        stream = io.BytesIO()
        write_frame(stream, b"one")
        write_frame(stream, b"")
        write_frame(stream, b"two" * 1000)
        stream.seek(0)
        self.assertEqual([read_frame(stream), read_frame(stream), read_frame(stream)], [b"one", b"", b"two" * 1000])
        with self.assertRaises(EOFError):
            read_frame(stream)

    def test_a_cut_frame_is_the_end_of_the_link(self):
        stream = io.BytesIO(b"\x00\x10abc")
        with self.assertRaises(EOFError):
            read_frame(stream)

    def test_an_oversized_packet_is_refused(self):
        with self.assertRaises(NetError):
            write_frame(io.BytesIO(), bytes(70000))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.router = Router(NET)
        self.host = self.router.address_for(None)
        self.alex = self.router.address_for("u-alex")
        self.sam = self.router.address_for("u-sam")
        self.boxes = {a: self.router.attach(a) for a in (self.host, self.alex, self.sam)}

    def drain(self, address):
        out = []
        while True:
            try:
                out.append(self.boxes[address].get_nowait())
            except queue.Empty:
                return out

    def test_a_packet_goes_to_its_one_member(self):
        packet = make_packet(self.alex, self.sam, b"move")
        self.assertEqual(self.router.route(self.alex, packet), 1)
        self.assertEqual(self.drain(self.sam), [packet])
        self.assertEqual(self.drain(self.host), [])
        self.assertEqual(self.drain(self.alex), [])

    def test_a_broadcast_goes_to_everyone_else(self):
        packet = make_packet(self.alex, "255.255.255.255", b"anyone hosting?")
        self.assertEqual(self.router.route(self.alex, packet), 2)
        self.assertEqual(self.drain(self.sam), [packet])
        self.assertEqual(self.drain(self.host), [packet])
        self.assertEqual(self.drain(self.alex), [], "not echoed back")

    def test_a_forged_sender_is_dropped(self):
        packet = make_packet(self.sam, self.host, b"I am Sam")
        self.assertEqual(self.router.route(self.alex, packet), 0)
        self.assertEqual(self.drain(self.host), [])
        self.assertEqual(self.router.dropped, 1)

    def test_traffic_for_elsewhere_goes_nowhere(self):
        self.assertEqual(self.router.route(self.alex, make_packet(self.alex, "8.8.8.8")), 0)
        self.assertEqual(self.router.route(self.alex, make_packet(self.alex, "10.77.12.200")), 0, "nobody there")

    def test_a_member_who_cannot_keep_up_loses_packets_instead_of_holding_others(self):
        packet = make_packet(self.alex, self.sam, b"x")
        for _ in range(gamenet.QUEUE_PACKETS + 10):
            self.router.route(self.alex, packet)
        self.assertEqual(len(self.drain(self.sam)), gamenet.QUEUE_PACKETS)
        self.assertEqual(self.router.dropped, 10)

    def test_a_reconnect_replaces_the_old_link_and_an_old_detach_does_not_remove_the_new(self):
        old = self.boxes[self.sam]
        new = self.router.attach(self.sam)
        self.router.detach(self.sam, old)
        self.assertEqual(self.router.route(self.alex, make_packet(self.alex, self.sam)), 1)
        self.assertEqual(new.qsize(), 1)


class PipeStream:
    """One direction of a fake socket: write() on one side, read() on the other, with a close."""

    def __init__(self):
        self._data = queue.Queue()
        self._buffer = b""
        self._closed = False

    def write(self, data):
        if self._closed:
            raise OSError("closed")
        self._data.put(bytes(data))

    def flush(self):
        pass

    def read(self, count):
        while len(self._buffer) < count:
            if self._closed and self._data.empty():
                break
            try:
                self._buffer += self._data.get(timeout=0.1)
            except queue.Empty:
                continue
        out, self._buffer = self._buffer[:count], self._buffer[count:]
        return out

    def close(self):
        self._closed = True


class PumpTests(unittest.TestCase):
    def test_a_member_link_carries_both_ways_and_keepalives(self):
        router = Router(NET)
        host, alex = router.address_for(None), router.address_for("u-alex")
        host_box = router.attach(host)
        to_host, to_member = PipeStream(), PipeStream()
        stop = threading.Event()
        inbox = router.attach(alex)
        thread = threading.Thread(target=gamenet.pump_link, args=(to_host, to_member, router, alex, inbox, stop),
                                  kwargs={"keepalive": 0.2}, daemon=True)
        thread.start()
        write_frame(to_host, make_packet(alex, host, b"hello host"))
        self.assertEqual(host_box.get(timeout=2), make_packet(alex, host, b"hello host"))
        router.route(host, make_packet(host, alex, b"hello alex"))
        frames = [read_frame(to_member) for _ in range(2)]
        self.assertIn(make_packet(host, alex, b"hello alex"), frames)
        time.sleep(0.5)
        self.assertEqual(read_frame(to_member), b"", "a keep-alive while idle")
        to_host.close()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive(), "the pump ends when the link closes")
        self.assertNotIn(str(alex), router.online(), "and the member is detached")
        stop.set()


class BridgeTests(unittest.TestCase):
    def test_only_game_network_packets_leave_the_adapter(self):
        tun = MemoryTun()
        sent = []
        stop = threading.Event()
        for packet in (make_packet("10.77.12.2", "10.77.12.3"), make_packet("10.77.12.2", "1.1.1.1"), b"\x60" + bytes(39),
                       make_packet("10.77.12.2", "10.77.12.255")):
            tun.outgoing.put(packet)
        thread = threading.Thread(target=bridge, args=(tun, sent.append, NET, stop), daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(sent) < 2:
            time.sleep(0.05)
        stop.set()
        thread.join(timeout=2)
        self.assertEqual([parse(p)[1] for p in sent], [ipaddress.IPv4Address("10.77.12.3"), ipaddress.IPv4Address("10.77.12.255")])


if __name__ == "__main__":
    unittest.main()
