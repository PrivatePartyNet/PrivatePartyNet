import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const L = require("../../privatepartynet/web/link.js");
require("../../privatepartynet/web/rtc.js");
const RTC = globalThis.PartyRTC;

test("my link: one late answer means nothing, silence moves the state on, three answers bring it back", () => {
  const meter = new L.Meter(0, 0);
  for (let t = 0; t < 20000; t += 2000) {
    if (t % 6000) meter.answered(80, t); else meter.missed(t);
    assert.equal(meter.update(t + 100, t + 100), false, `still ok at ${t} ms`);
  }
  assert.equal(meter.view().rtt, 80);
  assert.ok(meter.view().loss > 0.3 && meter.view().loss < 0.5);
  const last = 18000;
  assert.equal(meter.update(last + 4500, last + 4500), true);
  assert.equal(meter.state, "unstable");
  assert.equal(meter.update(last + 10500, last + 10500), true);
  assert.deepEqual([meter.state, meter.view().rtt], ["reconnecting", null]);
  for (const [n, t] of [[1, 40000], [2, 42000], [3, 44000]]) {
    meter.answered(90, t);
    meter.update(t + 10, t + 10);
    assert.equal(meter.state, n === 3 ? "ok" : "reconnecting");
  }
  assert.equal(meter.since, 44);
});

test("reconnecting waits 0.5 s, then longer, at most 10 s, with jitter", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 9].map((n) => L.backoff(n, 0.5)), [500, 1000, 2000, 4000, 8000, 10000, 10000]);
  assert.equal(L.backoff(0, 0), 400);
  assert.equal(L.backoff(9, 1), 12000);
});

test("after a dropout the page asks only for the chat it missed", () => {
  assert.equal(L.sinceParam({ general: 12, "voice-text": 3, empty: 0, "bad id!": 4 }), "general:12,voice-text:3");
  assert.equal(L.sinceParam({}), "");
});

test("the banner says what is happening, and what to do once the place has ended", () => {
  assert.equal(L.banner("reconnecting", 12400), "Reconnecting… (12 s)");
  assert.equal(L.banner("back", 0), "Reconnected");
  assert.match(L.banner("lost", 0, 120000), /could not be restored within 120 seconds.*Rejoin/);
  assert.equal(L.banner("ok", 0), null);
});

test("a screen goes smaller only while a link is having a hard time", () => {
  assert.equal(L.linkIsBad({ state: "ok", loss: 0.05 }), false);
  assert.equal(L.linkIsBad({ state: "ok", loss: 0.3 }), true);
  assert.equal(L.linkIsBad({ state: "unstable", loss: 0 }), true);
  assert.equal(L.linkIsBad(null), false);
});

// A stand-in RTCPeerConnection that records what is done to it.
function fakePeer(remoteSdp) {
  const calls = [];
  return {
    calls,
    signalingState: "stable",
    iceGatheringState: "complete",
    remoteDescription: remoteSdp ? { type: "offer", sdp: remoteSdp } : null,
    localDescription: null,
    async createOffer(options) { calls.push(["createOffer", options]); return { type: "offer", sdp: SDP("AA:BB") }; },
    async createAnswer() { calls.push(["createAnswer"]); return { type: "answer", sdp: SDP("AA:BB") }; },
    async setLocalDescription(d) {
      calls.push(["setLocal", d.type]);
      if (d.type === "rollback") { this.signalingState = "stable"; return; }
      this.localDescription = d;
      this.signalingState = d.type === "offer" ? "have-local-offer" : "stable";
    },
    async setRemoteDescription(d) { calls.push(["setRemote", d.type]); this.remoteDescription = d; },
    addEventListener() {}, removeEventListener() {},
  };
}
const SDP = (fp) => ["v=0", "m=audio 9 UDP/TLS/RTP/SAVPF 111", "a=rtpmap:111 opus/48000/2", "a=fmtp:111 minptime=10",
  `a=fingerprint:sha-256 ${fp}`, ""].join("\r\n");

test("an ICE restart is a new offer with fresh ICE on the same connection, an unanswered one taken back first", async () => {
  const pc = fakePeer(SDP("AA:BB"));
  pc.signalingState = "have-local-offer";
  const sent = [];
  assert.equal(await RTC.restartIce(pc, async (sdp) => sent.push(sdp)), true);
  assert.deepEqual(pc.calls.map((c) => c[0] === "createOffer" ? ["createOffer", c[1].iceRestart] : c),
    [["setLocal", "rollback"], ["createOffer", true], ["setLocal", "offer"]]);
  assert.equal(sent.length, 1);
  assert.match(sent[0], /useinbandfec=1/, "Opus keeps its in-band FEC through the restart");
});

test("a restart offer is answered on the connection that is there; a reloaded page's offer is not", async () => {
  const pc = fakePeer(SDP("AA:BB"));
  const sent = [];
  assert.equal(await RTC.answerInPlace(pc, SDP("aa:bb"), async (sdp) => sent.push(sdp)), true);
  assert.deepEqual(pc.calls.map((c) => c[0]), ["setRemote", "createAnswer", "setLocal"]);
  assert.equal(sent.length, 1);
  const other = fakePeer(SDP("AA:BB"));
  assert.equal(await RTC.answerInPlace(other, SDP("CC:DD"), async () => assert.fail("nothing is sent")), false);
  assert.deepEqual(other.calls, [], "a new certificate: the caller makes the connection over");
  assert.equal(RTC.sameEndpoint(null, SDP("AA:BB")), false);
});

test("a restart that cannot be delivered yet (the party is still away) rejects, so the page tries again", async () => {
  const pc = fakePeer(SDP("AA:BB"));
  await assert.rejects(RTC.restartIce(pc, async () => { throw new Error("/signal returned 409"); }));
  assert.equal(pc.signalingState, "have-local-offer");
  await RTC.restartIce(pc, async () => {});
  assert.equal(pc.calls.filter((c) => c[1] === "rollback").length, 1, "the next try takes the old offer back first");
});
