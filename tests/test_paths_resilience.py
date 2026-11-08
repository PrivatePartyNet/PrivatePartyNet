"""The game network's paths through bad internet (3.3): a burst of loss does not drop a way, an outage of the direct
way falls over to the relay without anything above noticing but the path's kind, and the path goes back to the direct
way once it has answered for a while. With pathway.PathManager on natsim's routers."""

import time
import unittest

import natsim
from privatepartynet import pathway
from test_paths_ipv6 import Base, Member, mesh, wait_for


class ResilienceTests(Base):
    def pair(self):
        v4, _ = self.relay()
        a = Member(self, natsim.SYMMETRIC, natsim.V6_STATEFUL, relay_config=self.vps(v4))
        b = Member(self, natsim.SYMMETRIC, natsim.V6_STATEFUL, relay_config=self.vps(v4))
        mesh(a, b)
        self.connects(a, b, "ipv6")
        return a, b

    def flowing(self, a, b, tag, seconds=5):
        a.paths[b.id].send(tag)
        return wait_for(lambda: tag in b.got, seconds)

    def test_thirty_percent_loss_for_twenty_seconds_keeps_the_direct_way(self):
        a, b = self.pair()
        changes = []
        a.paths[b.id].on_change = changes.append
        b.pc.v6_router.loss = 0.3
        ends = time.monotonic() + 20
        while time.monotonic() < ends:
            self.assertEqual(self.kinds(a, b)[0], "ipv6", a.manager.state())
            time.sleep(0.5)
        b.pc.v6_router.loss = 0.0
        self.assertTrue(self.flowing(a, b, b"after the loss"))
        self.assertNotIn("vps", changes, "a burst of loss is not a reason to leave the direct way")

    def test_an_outage_of_the_direct_way_falls_over_to_the_relay_and_comes_back(self):
        a, b = self.pair()
        b.pc.v6_router.down = True  # b's IPv6 goes away; its IPv4, which reaches the relay, does not
        self.assertTrue(wait_for(lambda: self.kinds(a, b) == ("vps", "vps"), pathway.DEAD_SECONDS + 15),
                        (self.kinds(a, b), a.manager.state()))
        self.assertTrue(self.flowing(a, b, b"through the relay"), "nothing above the path had to start over")
        self.assertTrue(self.flowing(b, a, b"and back"))
        b.pc.v6_router.down = False
        self.assertTrue(wait_for(lambda: self.kinds(a, b) == ("ipv6", "ipv6"), 30), (self.kinds(a, b), a.manager.state()))
        self.assertTrue(self.flowing(a, b, b"direct again"))

    def test_a_better_way_must_answer_a_few_times_before_the_path_moves_back(self):
        route = pathway._Route("ipv6", address=("2001:db8::1", 1))
        route.last_reply, route.missed = 100.0, 0
        self.assertTrue(route.alive(100.0 + pathway.DEAD_SECONDS + 1), "quiet, but too few probes lost to call it dead")
        route.nonces = {b"%d" % n: 100.0 + n for n in range(pathway.LOST_PROBES)}
        route.note_lost(100.0 + pathway.DEAD_SECONDS + 5)
        self.assertFalse(route.alive(100.0 + pathway.DEAD_SECONDS + 5))
        self.assertEqual(route.streak, 0)

    def test_a_lost_probe_of_the_way_in_use_is_followed_by_another_soon(self):
        route = pathway._Route("ipv6", address=("2001:db8::1", 1))
        route.last_reply, route.missed, route.rtt = 100.0, 0, 0.05  # a measured round trip: a probe is lost after 1 s
        route.nonces = {b"a": 102.0}  # the probe at 102 was lost, and the next is due PROBE_ACTIVE later
        route.next_probe = 102.0 + pathway.PROBE_ACTIVE
        pathway.ask_again_soon(route, 102.5)
        self.assertEqual(route.next_probe, 102.0 + pathway.PROBE_ACTIVE, "not late yet: it may still be answered")
        pathway.ask_again_soon(route, 103.2)
        self.assertEqual(route.missed, 1)
        self.assertLessEqual(route.next_probe, 103.2 + pathway.PROBE_FAST)


if __name__ == "__main__":
    unittest.main()
