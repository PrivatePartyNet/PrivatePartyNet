import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const N = require("../../privatepartynet/web/netstats.js");

const mids = ["0", "1", "2", "3"];

function report({ inbound = {}, outbound = {}, rtt = 0.004 } = {}) {
  const entries = [{ type: "candidate-pair", nominated: true, state: "succeeded", currentRoundTripTime: rtt }];
  for (const [mid, fields] of Object.entries(inbound)) {
    entries.push({ type: "inbound-rtp", kind: "video", mid, isRemote: false, ...fields });
  }
  for (const [mid, fields] of Object.entries(outbound)) {
    entries.push({ type: "outbound-rtp", kind: "video", mid, isRemote: false, ...fields });
  }
  return entries;
}

test("a stats report is reduced to slots by mid", () => {
  const sample = N.normalize(
    report({
      inbound: { 0: { bytesReceived: 1000, framesDecoded: 10, frameWidth: 800, frameHeight: 450, totalDecodeTime: 0.2 } },
      outbound: { 1: { bytesSent: 500, framesEncoded: 5, frameWidth: 400, frameHeight: 225, totalEncodeTime: 0.1, qualityLimitationReason: "cpu" } },
    }),
    mids,
    1000,
  );
  assert.equal(sample.rtt, 4);
  assert.equal(sample.inn[0].w, 800);
  assert.equal(sample.out[1].limit, "cpu");
  assert.equal(sample.inn[1], undefined);
});

test("entries for an unknown mid, remote stats and audio are ignored", () => {
  const entries = report({ inbound: { 9: { bytesReceived: 1 } } });
  entries.push({ type: "inbound-rtp", kind: "audio", mid: "0", bytesReceived: 5 });
  entries.push({ type: "inbound-rtp", kind: "video", mid: "0", isRemote: true, bytesReceived: 5 });
  const sample = N.normalize(entries, mids, 0);
  assert.deepEqual(sample.inn, {});
});

function twoSamples() {
  const a = N.normalize(
    report({
      inbound: { 0: { bytesReceived: 0, packetsReceived: 0, packetsLost: 0, framesDecoded: 0, frameWidth: 800, frameHeight: 450, totalDecodeTime: 0 } },
      outbound: { 1: { bytesSent: 0, framesEncoded: 0, frameWidth: 400, frameHeight: 225, totalEncodeTime: 0, qualityLimitationReason: "none" } },
    }),
    mids,
    0,
  );
  const b = N.normalize(
    report({
      inbound: { 0: { bytesReceived: 250000, packetsReceived: 990, packetsLost: 10, framesDecoded: 30, frameWidth: 800, frameHeight: 450, totalDecodeTime: 0.24 } },
      outbound: { 1: { bytesSent: 50000, framesEncoded: 20, frameWidth: 400, frameHeight: 225, totalEncodeTime: 0.12, qualityLimitationReason: "bandwidth" } },
    }),
    mids,
    1000,
  );
  return [a, b];
}

test("two reports give rates, frame rates and the CPU share of the whole PC", () => {
  const [a, b] = twoSamples();
  const d = N.diff(a, b, { cores: 24, activeIn: new Set([0]), activeOut: new Set([1]) });
  assert.equal(d.down.length, 1);
  assert.equal(Math.round(d.down[0].kbps), 2000);
  assert.equal(d.down[0].fps, 30);
  assert.ok(Math.abs(d.down[0].cpu - (0.24 / 24) * 100) < 1e-9);
  assert.equal(Math.round(d.up[0].kbps), 400);
  assert.equal(d.up[0].fps, 20);
  assert.equal(d.up[0].limit, "bandwidth");
  assert.ok(Math.abs(d.loss - 1) < 1e-9, "10 lost of 1000");
  assert.equal(d.rtt, 4);
});

test("a slot nobody is sharing is left out even if it has counters", () => {
  const [a, b] = twoSamples();
  const d = N.diff(a, b, { cores: 4, activeIn: new Set(), activeOut: new Set([1]) });
  assert.equal(d.down.length, 0);
  assert.equal(d.up.length, 1);
});

test("with no earlier report there are no rates yet", () => {
  const [, b] = twoSamples();
  const d = N.diff(null, b, { cores: 4 });
  assert.deepEqual(d.down, []);
  assert.deepEqual(d.up, []);
});

test("a stalled report with the same timestamp does not divide by zero", () => {
  const [a] = twoSamples();
  assert.deepEqual(N.diff(a, a, { cores: 4 }).down, []);
});

test("only slots with something flowing count, and the totals use the best of each", () => {
  const total = N.total([
    { slot: 0, kbps: 1500, fps: 24, w: 1280, h: 720, cpu: 1.2 },
    { slot: 1, kbps: 600, fps: 30, w: 1920, h: 1080, cpu: 0.5 },
    { slot: 2, kbps: 0, fps: 0, w: 640, h: 360, cpu: 0 },
  ]);
  assert.equal(total.count, 2);
  assert.equal(total.kbps, 2100);
  assert.equal(total.fps, 30);
  assert.equal(total.w, 1920);
  assert.ok(Math.abs(total.cpu - 1.7) < 1e-9);
  assert.equal(N.total([{ slot: 0, kbps: 0, fps: 0, w: 1, h: 1, cpu: 0 }]), null);
  assert.equal(N.total([]), null);
});

test("an encoder limit is reported when any slot has one", () => {
  const total = N.total([{ slot: 0, kbps: 500, fps: 20, w: 800, h: 450, cpu: 1, limit: "none" }, { slot: 1, kbps: 500, fps: 20, w: 800, h: 450, cpu: 1, limit: "cpu" }]);
  assert.equal(total.limit, "cpu");
});

test("numbers are formatted to be read at a glance", () => {
  assert.equal(N.formatRate(315), "315 kbps");
  assert.equal(N.formatRate(1200), "1.2 Mbps");
  assert.equal(N.formatRate(NaN), "n/a");
  assert.equal(N.formatCpu(0.04), "<0.1%");
  assert.equal(N.formatCpu(9.86), "9.9%");
  assert.equal(N.formatSize(1599, 669), "1599×669");
  assert.equal(N.formatSize(0, 0), "");
});

test("the most detailed level that fits is chosen, and the last one when none do", () => {
  const levels = [3, 2, 1, 0];
  assert.equal(N.highestFitting(levels, (l) => l <= 2), 2);
  assert.equal(N.highestFitting(levels, () => true), 3);
  assert.equal(N.highestFitting(levels, () => false), 0);
});

test("the hover card says which way a connection goes, IPv6 and the relay server included", () => {
  const pair = (local, remote) => [
    { type: "candidate-pair", nominated: true, state: "succeeded", localCandidateId: "L", remoteCandidateId: "R" },
    { type: "local-candidate", id: "L", ...local },
    { type: "remote-candidate", id: "R", ...remote },
  ];
  assert.equal(N.way(pair({ candidateType: "host" }, { candidateType: "srflx", address: "2001:db8::7" })), "ipv6");
  assert.equal(N.way(pair({ candidateType: "relay" }, { candidateType: "srflx", address: "203.0.113.5" })), "relay");
  assert.equal(N.way(pair({ candidateType: "host" }, { candidateType: "host", address: "192.168.1.20" })), "lan");
  assert.equal(N.way(pair({ candidateType: "srflx" }, { candidateType: "prflx", address: "203.0.113.5" })), "direct");
  assert.equal(N.way([]), null, "nothing chosen yet");
  assert.equal(N.wayText("ipv6"), "Direct over IPv6");
  assert.equal(N.wayText("relay"), "Through the relay server");
  const sample = N.normalize(pair({ candidateType: "host" }, { candidateType: "host", address: "2001:db8::7" }), [], 0);
  assert.equal(N.diff(null, sample).way, "ipv6");
});
