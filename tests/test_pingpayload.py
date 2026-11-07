"""The ping display (web/pingview.js) reads each member's "link" from the party's list. Whatever the party puts there
reaches every page as it is, on the roster events of the page's event stream."""

import unittest
from unittest import mock

import test_server
from test_server import online_names, read_until


class LinkPassThroughTests(unittest.TestCase):
    # A real party on this PC, with test_server's helpers (but not its tests).
    base = test_server.PartyTests
    setUp, open, join, host_cookie, stream = base.setUp, base.open, base.join, base.host_cookie, base.stream

    def test_each_members_link_reaches_the_page_as_the_party_gives_it(self):
        link = {"state": "unstable", "rtt": 41, "loss": 0.02, "since": 1793865000, "path": "punched"}
        public = self.party._public
        with mock.patch.object(self.party, "_public", lambda member: {**public(member), "link": link}):
            host = self.stream(self.host_cookie())
            read_until(host, "hello")
            alex = self.stream(self.join("Alex"))
            read_until(alex, "hello")
            for stream in (host, alex):
                roster = read_until(stream, "roster", lambda e: len(online_names(e)) == 2)
                self.assertEqual([m["link"] for m in roster["members"]], [link, link])

    def test_a_member_without_a_link_comes_through_without_one(self):
        # A host on an older PrivatePartyNet sends none: the page shows the ping as not known (pingview.pick).
        host = self.stream(self.host_cookie())
        read_until(host, "hello")
        roster = read_until(host, "roster")
        self.assertTrue(all("link" not in m or isinstance(m["link"], dict) for m in roster["members"]))


if __name__ == "__main__":
    unittest.main()
