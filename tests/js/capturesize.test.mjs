import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const C = require("../../privatepartynet/web/capturesize.js");

const ultrawide = { width: 3440, height: 1440 };

test("a viewer with a small tile needs a small picture, in the screen's own shape", () => {
  // a 1000x600 tile shows a 3440x1440 screen at 1000x419
  const h = C.wantedHeight(ultrawide, { mode: "fit", width: 1000, height: 600 });
  assert.ok(Math.abs(h - 1440 / (3440 / 1000)) < 0.5);
});

test("a tall narrow tile is limited by its height, not its width", () => {
  const h = C.wantedHeight({ width: 1920, height: 1080 }, { mode: "fit", width: 4000, height: 300 });
  assert.equal(h, 300);
});

test("nothing is ever asked for beyond the screen's own height", () => {
  assert.equal(C.wantedHeight(ultrawide, { mode: "fit", width: 9000, height: 9000 }), 1440);
  assert.equal(C.wantedHeight(ultrawide, { mode: "percent", percent: 100 }), 1440);
});

test("a percentage is a share of the real size, whatever the capture size is now", () => {
  assert.equal(C.wantedHeight(ultrawide, { mode: "percent", percent: 50 }), 720);
  assert.equal(C.wantedHeight(ultrawide, { mode: "percent", percent: 25 }), 360);
});

test("a viewer who has not said yet is assumed to want everything, and one who hid the stream wants nothing", () => {
  assert.equal(C.wantedHeight(ultrawide, undefined), 1440);
  assert.equal(C.wantedHeight(ultrawide, { mode: "off" }), 0);
});

test("the capture covers the most demanding viewer", () => {
  const need = C.neededHeight(ultrawide, [
    { mode: "fit", width: 800, height: 500 },
    { mode: "percent", percent: 50 },
    { mode: "off" },
  ], null);
  assert.equal(need, 720);
});

test("the sender's own quality limit caps it", () => {
  assert.equal(C.neededHeight(ultrawide, [{ mode: "percent", percent: 100 }], 720), 720);
});

test("with nobody watching the smallest size is enough", () => {
  assert.equal(C.neededHeight(ultrawide, [], null), 240);
  assert.equal(C.neededHeight(ultrawide, [{ mode: "off" }], null), 240);
});

test("a need becomes the smallest standard size that covers it", () => {
  assert.equal(C.stepFor(100, 1440), 240);
  assert.equal(C.stepFor(241, 1440), 360);
  assert.equal(C.stepFor(719, 1440), 720);
  assert.equal(C.stepFor(720, 1440), 720);
  assert.equal(C.stepFor(721, 1440), 1080);
});

test("when the standard sizes would not save anything, the screen is captured unscaled", () => {
  assert.equal(C.stepFor(1440, 1440), null);
  assert.equal(C.stepFor(1400, 1440), null);
  assert.equal(C.stepFor(1100, 1080), null);
  assert.equal(C.stepFor(500, 0), null);
  assert.equal(C.stepFor(700, 720), null, "a 720 screen is not captured at 720 by a step; it is the screen");
});

test("a small screen is never captured above its own size", () => {
  assert.equal(C.stepFor(500, 600), null);
  assert.equal(C.stepFor(300, 600), 360);
});

test("a larger need is taken at once", () => {
  const g = new C.Governor(480);
  assert.deepEqual(g.decide(720, 0), { height: 720, changed: true });
  assert.deepEqual(g.decide(null, 10), { height: null, changed: true });
});

test("a smaller need waits until it has lasted", () => {
  const g = new C.Governor(null);
  assert.equal(g.decide(480, 0).changed, false);
  assert.equal(g.decide(480, 4000).changed, false);
  const settled = g.decide(480, C.SETTLE_MS);
  assert.deepEqual(settled, { height: 480, changed: true });
  assert.equal(g.decide(480, C.SETTLE_MS + 1000).changed, false);
});

test("a need that bounces is settled at the largest it reached, and a rise resets the wait", () => {
  const g = new C.Governor(1080);
  g.decide(360, 0);
  g.decide(720, 1000);
  g.decide(360, 2000);
  const settled = g.decide(360, C.SETTLE_MS);
  assert.deepEqual(settled, { height: 720, changed: true });
  const h = new C.Governor(720);
  h.decide(360, 0);
  h.decide(720, 3000); // the need went back up to what it already is: the wait starts over
  assert.equal(h.decide(360, 4000).changed, false);
  assert.equal(h.decide(360, 4000 + C.SETTLE_MS - 1).changed, false);
  assert.equal(h.decide(360, 4000 + C.SETTLE_MS).changed, true);
});

test("asking for what it already has changes nothing", () => {
  const g = new C.Governor(720);
  assert.deepEqual(g.decide(720, 0), { height: 720, changed: false });
  assert.deepEqual(new C.Governor(null).decide(null, 0), { height: null, changed: false });
});
