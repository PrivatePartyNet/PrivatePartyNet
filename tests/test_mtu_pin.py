"""The game network's packet sizes, read from both sides: the helper's adapter MTU (wghelper, Go) and what the paths are
told to carry (pathway.py). If either changes without the other, games stall on paths that seem to work."""

import re
import unittest
from pathlib import Path

from privatepartynet import gameclient, pathway
from privatepartynet import relayproto as rp

WGHELPER = Path(__file__).resolve().parents[1] / "wghelper"


def go_constants():
    """Every integer constant the helper's Go files define, worked out from their arithmetic (and len of string ones)."""
    raw, strings = {}, {}
    for path in sorted(WGHELPER.glob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r"(?ms)^const \($(.*?)^\)", text) + re.findall(r"(?m)^const (\w+\s*=.*)$", text):
            for line in block.splitlines():
                match = re.match(r"\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*(?://.*)?$", line)
                if not match:
                    continue
                name, value = match.groups()
                if value.startswith('"'):
                    strings[name] = value.strip('"')
                else:
                    raw[name] = value
    values = {}
    for _ in range(len(raw) + 1):
        for name, expression in raw.items():
            if name in values:
                continue
            expression = re.sub(r"len\((\w+)\)", lambda m: str(len(strings[m.group(1)])) if m.group(1) in strings
                                else m.group(0), expression)
            if re.fullmatch(r"[\w\s+\-*/()]+", expression):
                names = re.findall(r"[A-Za-z_]\w*", expression)
                if all(n in values for n in names):
                    values[name] = eval(expression, {"__builtins__": {}}, dict(values))  # arithmetic on known integers
            elif re.fullmatch(r"\d+", expression):
                values[name] = int(expression)
    return values


class MtuPinTests(unittest.TestCase):
    def setUp(self):
        if not WGHELPER.is_dir():
            self.skipTest("no wghelper in this tree")
        self.go = go_constants()

    def test_the_paths_probe_for_exactly_what_the_adapter_can_send(self):
        largest = self.go["MTU"] + self.go["fanoutExtra"] + self.go["WireGuardOv"]
        self.assertEqual(largest, pathway.GAME_MAX_DATAGRAM,
                         "wghelper's MTU moved: set pathway.GAME_MAX_DATAGRAM to MTU + fan-out wrapper + WireGuard")
        self.assertEqual(self.go["MTU"], gameclient.GAME_MTU)

    def test_the_helper_counts_the_paths_framing_as_it_really_is(self):
        self.assertEqual(self.go["PathHeader"], 1 + rp.HEADER, "the DATA marker and a relay's SEND/DELIVER header")
        on_wire = pathway.FULL_SIZE + rp.HEADER + self.go["UDPHeader"] + self.go["IPv6Header"]
        self.assertEqual(on_wire, self.go["MaxOnWire"], "a relayed full-size datagram over IPv6")
        self.assertLessEqual(pathway.GAME_MAX_DATAGRAM, pathway.MAX_DATAGRAM)


if __name__ == "__main__":
    unittest.main()
