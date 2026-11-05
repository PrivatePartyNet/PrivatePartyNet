import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const P = require("../../privatepartynet/web/pingview.js");

test("the colour follows the ping and the loss", () => {
  assert.equal(P.quality(0, 0), "good");
  assert.equal(P.quality(79.4, 0), "good");
  assert.equal(P.quality(80, 0), "fair");
  assert.equal(P.quality(149, 0.05), "fair", "5% loss exactly is not over 5%");
  assert.equal(P.quality(150, 0), "poor");
  assert.equal(P.quality(20, 0.051), "poor", "loss over 5% is poor however short the ping");
  assert.equal(P.quality(null, null), "unknown");
  assert.equal(P.quality(undefined, 0), "unknown");
  assert.equal(P.quality(NaN, 0), "unknown");
  assert.equal(P.quality(-1, 0), "unknown");
});

test("a ping is written in whole milliseconds", () => {
  assert.equal(P.text({ state: "ok", rtt: 23.4 }), "23 ms");
  assert.equal(P.text({ state: "ok", rtt: 0.4 }), "0 ms");
  assert.equal(P.text({ state: "ok", rtt: null }), "—");
  assert.equal(P.text({ state: "reconnecting", rtt: 23 }), "reconnecting…", "the state shows instead of the number");
  assert.equal(P.text({ state: "unstable", rtt: 23 }), "unstable");
  assert.equal(P.text({ state: "self" }), "", "the host's own row has nothing to measure");
});

const connected = { state: "connected", rtt: 12.6, loss: 1.5, jitter: 3.2, way: "ipv6" };

test("a direct connection to the member is the best source", () => {
  const got = P.pick({ member: { id: "b", link: { state: "ok", rtt: 40, loss: 0 } }, summary: connected });
  assert.deepEqual(got, { state: "ok", rtt: 12.6, loss: 0.015, jitter: 3.2, source: "direct", way: "ipv6" });
});

test("without one, the host's heartbeat stands in, through the host", () => {
  for (const summary of [undefined, { state: "connecting", rtt: null }, { state: "connected", rtt: null }]) {
    const got = P.pick({ member: { id: "b", link: { state: "ok", rtt: 41, loss: 0.02, path: "punched" } }, summary });
    assert.equal(got.source, "host");
    assert.equal(got.rtt, 41);
    assert.equal(got.loss, 0.02);
    assert.equal(P.wayText(got.way), "Direct through both routers");
  }
});

test("an older host sends no link: the ping is unknown, not zero", () => {
  const got = P.pick({ member: { id: "b" } });
  assert.equal(got.source, "none");
  assert.equal(got.rtt, null);
  assert.equal(P.quality(got.rtt, got.loss), "unknown");
  assert.equal(P.text(got), "—");
});

test("an unstable or reconnecting link shows instead of any number", () => {
  for (const state of ["unstable", "reconnecting"]) {
    const got = P.pick({ member: { id: "b", link: { state, rtt: 300, loss: 0.3 } }, summary: connected });
    assert.equal(got.state, state);
    assert.equal(P.text(got), state === "reconnecting" ? "reconnecting…" : "unstable");
  }
});

test("your own row is your ping to the host, and the host's own row is empty", () => {
  const mine = P.pick({ member: { id: "me" }, self: true, myLink: { state: "ok", rtt: 33, loss: 0 } });
  assert.deepEqual([mine.source, mine.rtt], ["mylink", 33]);
  const fallback = P.pick({ member: { id: "me", link: { state: "ok", rtt: 35, loss: 0 } }, self: true });
  assert.deepEqual([fallback.source, fallback.rtt], ["mylink", 35]);
  const host = P.pick({ member: { id: "me" }, self: true, selfIsHost: true });
  assert.equal(host.state, "self");
});

test("a member's row for the host falls back to their own link to the host", () => {
  // The host's own entry says 0 ms (it is the host); that is no ping to show anyone.
  const host = { id: "h", link: { state: "ok", rtt: 0, loss: 0 } };
  const got = P.pick({ member: host, isHost: true, myLink: { state: "ok", rtt: 52, loss: 0 } });
  assert.deepEqual([got.source, got.rtt], ["mylink", 52]);
  assert.equal(P.pick({ member: host, isHost: true }).rtt, null, "no ping yet, not 0 ms");
  assert.equal(P.pick({ member: host, isHost: true, summary: connected }).source, "direct");
});

test("the hover says the ping, jitter, loss, way and where the number comes from", () => {
  const direct = P.detail(P.pick({ member: { id: "b" }, summary: connected }), "Alex");
  assert.deepEqual(direct, [
    "Ping 13 ms  ·  jitter 3 ms  ·  loss 1.5%",
    "Direct over IPv6",
    "Measured on your own connection to Alex.",
  ]);
  const host = P.detail(P.pick({ member: { id: "b", link: { state: "ok", rtt: 41, loss: 0, path: "friend" } } }), "Alex");
  assert.deepEqual(host, [
    "Ping 41 ms  ·  loss 0.0%",
    "Through a member's PC",
    "Alex's ping to the host, which measures it every 2 seconds (you have no direct connection to Alex).",
  ]);
  const plain = P.detail(P.pick({ member: { id: "b", link: { state: "ok", rtt: 41, loss: 0 } } }), "Alex");
  assert.equal(plain[1], "Through the host");
  const ways = (path) => P.detail(P.pick({ member: { id: "b", link: { state: "ok", rtt: 41, loss: 0, path } } }), "A")[1];
  assert.equal(ways("direct-lan"), "Direct on the same network");
  assert.equal(ways("direct-ipv6"), "Direct over IPv6");
  assert.equal(ways("direct"), "Direct to a public address or an opened port");
  assert.equal(ways("punched"), "Direct through both routers");
  assert.equal(ways("relay"), "Through a relay (the relay server or a member's PC)");
  assert.equal(ways("friend"), "Through a member's PC");
  const webrtc = (way) => P.detail(P.pick({ member: { id: "b" }, summary: { ...connected, way } }), "A")[1];
  assert.equal(webrtc("relay"), "Through the relay server", "WebRTC's relay is the relay server (TURN)");
  assert.equal(webrtc("lan"), "Direct on the same network");
  const mine = P.detail(P.pick({ member: { id: "me" }, self: true, myLink: { state: "ok", rtt: 33, loss: 0 } }), "you");
  assert.equal(mine[mine.length - 1], "Your ping to the host, which measures it every 2 seconds.");
  const unknown = P.detail(P.pick({ member: { id: "b" } }), "Alex");
  assert.deepEqual(unknown, ["Ping not known yet (the host's PrivatePartyNet may be older)."]);
  const shaky = P.detail(P.pick({ member: { id: "b", link: { state: "reconnecting", rtt: null, loss: 0.4 } } }), "Alex");
  assert.equal(shaky[0], "Alex's connection to the host is reconnecting.");
});

test("the control panel's game network line has the same colours", () => {
  assert.deepEqual(P.gameLine({ name: "Alex", way: "direct over IPv6", rtt: 23 }),
    { text: "Alex, direct over IPv6, 23 ms", quality: "good" });
  assert.deepEqual(P.gameLine({ name: "Bea", way: "through a member's PC", rtt: 160 }),
    { text: "Bea, through a member's PC, 160 ms", quality: "poor" });
  assert.deepEqual(P.gameLine({ name: "Cy", way: "not connected yet", rtt: null }),
    { text: "Cy, not connected yet", quality: "unknown" });
});

test("the connection bar's numbers read the same as the members list's", () => {
  assert.equal(P.lossText(0.015), "1.5%");
  assert.equal(P.lossText(null), "n/a");
  assert.equal(P.summaryLine({ rtt: 12.6, loss: 1.5 }), "Ping 13 ms  ·  loss 1.5%");
  assert.equal(P.summaryLine({ rtt: null, loss: 0 }), "Ping n/a  ·  loss 0.0%");
  assert.equal(P.summaryQuality({ state: "connected", rtt: 160, loss: 0 }), "poor");
  assert.equal(P.summaryQuality({ state: "connecting", rtt: 10, loss: 0 }), "unknown", "only a live connection has a ping");
});

test("a member on an older app, whom the host does not ping, is grey and never unstable", () => {
  const old = { id: "b", link: { state: "ok", rtt: null, loss: null, since: 1793865000 } };
  const got = P.pick({ member: old });
  assert.equal(P.quality(got.rtt, got.loss), "unknown");
  assert.equal(P.text(got), "—");
  assert.deepEqual(P.detail(got, "Alex"),
    ["Ping not known: the host has no heartbeat with Alex (their PrivatePartyNet may be older)."]);
  assert.equal(P.pick({ member: old, summary: connected }).source, "direct", "their own connection still gives a ping");
});

test("a link over a VPN says so", () => {
  const got = P.pick({ member: { id: "b", link: { state: "ok", rtt: 30, loss: 0, path: "vpn" } } });
  assert.equal(P.detail(got, "Alex")[1], "Direct over a VPN (Radmin VPN, Hamachi or Tailscale)");
});
