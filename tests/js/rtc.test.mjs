import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
require("../../privatepartynet/web/rtc.js");
const RTC = globalThis.PartyRTC;

const OFFER = [
  "v=0", "o=- 1 2 IN IP4 127.0.0.1", "s=-", "t=0 0",
  "m=audio 9 UDP/TLS/RTP/SAVPF 111 63",
  "a=rtpmap:111 opus/48000/2",
  "a=fmtp:111 minptime=10;useinbandfec=1",
  "a=rtpmap:63 red/48000/2",
  "m=video 9 UDP/TLS/RTP/SAVPF 96",
  "a=rtpmap:96 VP8/90000",
  "",
].join("\r\n");

function opusParams(sdp) {
  const line = sdp.split("\r\n").find((l) => l.startsWith("a=fmtp:111 "));
  return Object.fromEntries(line.slice("a=fmtp:111 ".length).split(";").map((p) => p.split("=")));
}

test("Opus is set to 32 kbps mono with error correction and silence suppression", () => {
  const params = opusParams(RTC.tuneOpus(OFFER));
  assert.equal(params.maxaveragebitrate, "32000");
  assert.equal(params.stereo, "0");
  assert.equal(params["sprop-stereo"], "0");
  assert.equal(params.useinbandfec, "1");
  assert.equal(params.usedtx, "1");
  assert.equal(params.minptime, "10");
});

test("a line the browser already wrote keeps its other settings and is not duplicated", () => {
  const out = RTC.tuneOpus(OFFER.replace("minptime=10;useinbandfec=1", "minptime=20;useinbandfec=0;cbr=1"));
  const params = opusParams(out);
  assert.equal(params.cbr, "1");
  assert.equal(params.minptime, "10", "ours wins where both set it");
  assert.equal(params.useinbandfec, "1");
  assert.equal(out.split("\r\n").filter((l) => l.startsWith("a=fmtp:111 ")).length, 1);
});

test("an offer with Opus and no fmtp line gets one, right after the rtpmap", () => {
  const bare = OFFER.replace("a=fmtp:111 minptime=10;useinbandfec=1\r\n", "");
  const out = RTC.tuneOpus(bare).split("\r\n");
  const at = out.indexOf("a=rtpmap:111 opus/48000/2");
  assert.ok(out[at + 1].startsWith("a=fmtp:111 "));
  assert.equal(opusParams(out.join("\r\n")).usedtx, "1");
});

test("nothing else in the offer changes", () => {
  const out = RTC.tuneOpus(OFFER).split("\r\n").filter((l) => !l.startsWith("a=fmtp:111 "));
  assert.deepEqual(out, OFFER.split("\r\n").filter((l) => !l.startsWith("a=fmtp:111 ")));
});

test("an offer with no Opus is returned as it was", () => {
  const video = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 VP8/90000\r\n";
  assert.equal(RTC.tuneOpus(video), video);
});

test("the stats counters include voice", () => {
  const stats = new Map([
    ["a", { type: "inbound-rtp", kind: "audio", bytesReceived: 4000, isRemote: false }],
    ["b", { type: "outbound-rtp", kind: "audio", bytesSent: 2500, isRemote: false }],
    ["c", { type: "inbound-rtp", kind: "video", bytesReceived: 9000, isRemote: false, frameWidth: 640, frameHeight: 360, framesPerSecond: 30 }],
  ]);
  const sample = RTC.readReport(stats);
  assert.equal(sample.bytes["down-audio"], 4000);
  assert.equal(sample.bytes["up-audio"], 2500);
  assert.equal(sample.bytes["down-video"], 9000);
});

test("STUN is off on one network and on once someone joined from outside, with no relay", () => {
  assert.equal(RTC.stunOn(), false);
  RTC.setStun(true);
  assert.equal(RTC.stunOn(), true);
  RTC.setStun(false);
  assert.equal(RTC.stunOn(), false);
});
