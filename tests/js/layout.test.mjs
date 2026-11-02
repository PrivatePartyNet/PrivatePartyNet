import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const L = require("../../privatepartynet/web/layout.js");

const stage = { w: 1200, h: 700 };
const inside = (r, s = stage) => r.x >= 0 && r.y >= 0 && r.x + r.w <= s.w + 1 && r.y + r.h <= s.h + 1;
const overlap = (a, b) => L.overlaps(a, b, 0);

test("one picture fills the stage in its own shape, centered", () => {
  const [r] = L.autoArrange([{ key: "a", aspect: 16 / 9 }], stage);
  assert.ok(inside(r));
  assert.ok(Math.abs(r.w / r.h - 16 / 9) < 0.02);
  assert.ok(Math.abs(r.x + r.w / 2 - stage.w / 2) <= 1);
  assert.ok(Math.abs(r.y + r.h / 2 - stage.h / 2) <= 1);
});

test("an ultrawide picture is not squeezed into 16 by 9", () => {
  const [r] = L.autoArrange([{ key: "a", aspect: 3440 / 1440 }], stage);
  assert.ok(Math.abs(r.w / r.h - 3440 / 1440) < 0.03);
  assert.ok(r.w > 1100, "it uses the width of the stage");
});

test("several pictures never overlap and all stay inside the stage", () => {
  for (let n = 1; n <= 9; n += 1) {
    const items = Array.from({ length: n }, (_, i) => ({ key: "k" + i, aspect: i % 2 ? 16 / 9 : 3440 / 1440 }));
    const rects = L.autoArrange(items, stage);
    assert.equal(rects.length, n);
    rects.forEach((r, i) => {
      assert.ok(inside(r), `tile ${i} of ${n} is inside`);
      rects.slice(i + 1).forEach((o) => assert.ok(!overlap(r, o), `tiles ${r.key} and ${o.key} of ${n} do not overlap`));
    });
    assert.deepEqual(rects.map((r) => r.key), items.map((i) => i.key), "the order is kept");
  }
});

test("the last row is centered", () => {
  const items = Array.from({ length: 3 }, (_, i) => ({ key: "k" + i, aspect: 16 / 9 }));
  const rects = L.autoArrange(items, { w: 900, h: 700 });
  const lastRow = rects.filter((r) => r.y === Math.max(...rects.map((q) => q.y)));
  const left = Math.min(...lastRow.map((r) => r.x));
  const right = Math.max(...lastRow.map((r) => r.x + r.w));
  assert.ok(Math.abs(left - (900 - right)) <= 2, "equal space left and right");
});

test("a size cap keeps a picture from growing past it", () => {
  const [r] = L.autoArrange([{ key: "a", aspect: 16 / 9, maxW: 400 }], stage);
  assert.ok(r.w <= 401);
});

test("an empty list and a tiny stage do not break", () => {
  assert.deepEqual(L.autoArrange([], stage), []);
  const rects = L.autoArrange([{ key: "a", aspect: 1.5 }, { key: "b", aspect: 1.5 }], { w: 60, h: 60 });
  assert.equal(rects.length, 2);
});

test("a free spot does not touch another tile and sits inside the stage", () => {
  const taken = [{ x: 10, y: 10, w: 400, h: 225 }];
  const spot = L.findFreeSpot({ w: 400, h: 225 }, taken, stage);
  assert.ok(inside(spot));
  assert.ok(!overlap(spot, taken[0]));
});

test("with no room left a new tile cascades but stays inside", () => {
  const taken = [{ x: 0, y: 0, w: 1200, h: 700 }];
  const spot = L.findFreeSpot({ w: 400, h: 225 }, taken, stage);
  assert.ok(inside(spot));
});

test("clampInto pulls a rectangle back inside and enforces the minimum", () => {
  const r = L.clampInto({ x: -50, y: 900, w: 100, h: 50 }, stage);
  assert.ok(inside(r));
  assert.ok(r.w >= L.MIN_W && r.h >= L.MIN_H);
  const big = L.clampInto({ x: 0, y: 0, w: 5000, h: 5000 }, stage);
  assert.ok(big.w <= stage.w && big.h <= stage.h);
});

test("dragging near the stage edge snaps to the margin", () => {
  const out = L.snapMove({ x: 14, y: 300, w: 300, h: 170 }, [], stage);
  assert.equal(out.x, L.PAD);
  assert.ok(out.guides.some((g) => g.axis === "v"));
});

test("dragging near another tile snaps to sit beside it with the gap", () => {
  const other = { x: 100, y: 100, w: 300, h: 170 };
  const out = L.snapMove({ x: other.x + other.w + L.GAP + 6, y: 105, w: 300, h: 170 }, [other], stage);
  assert.equal(out.x, other.x + other.w + L.GAP);
  assert.equal(out.y, other.y, "the tops line up too");
  assert.ok(out.guides.length >= 1);
});

test("far from everything nothing snaps", () => {
  const out = L.snapMove({ x: 400, y: 300, w: 300, h: 170 }, [{ x: 20, y: 20, w: 100, h: 100 }], stage);
  assert.equal(out.x, 400);
  assert.equal(out.y, 300);
  assert.deepEqual(out.guides, []);
});

test("a dragged tile is kept inside the stage", () => {
  const out = L.snapMove({ x: 1190, y: 690, w: 300, h: 170 }, [], stage);
  assert.ok(out.x + 300 <= stage.w);
  assert.ok(out.y + 170 <= stage.h);
});

test("snapping to the middle of the stage", () => {
  const out = L.snapMove({ x: (stage.w - 300) / 2 + 5, y: 300, w: 300, h: 170 }, [], stage);
  assert.equal(out.x, (stage.w - 300) / 2);
});

const base = { x: 200, y: 150, w: 400, h: 225 };

test("resizing never goes below the minimum size", () => {
  const { rect } = L.resize(base, "se", -1000, -1000, [], stage, {});
  assert.ok(rect.w >= L.MIN_W && rect.h >= L.MIN_H);
});

test("resizing from the west keeps the right edge where it was", () => {
  const { rect } = L.resize(base, "w", -60, 0, [], stage, {});
  assert.equal(rect.x + rect.w, base.x + base.w);
  assert.equal(rect.w, base.w + 60);
});

test("a locked shape keeps its proportions from a corner", () => {
  const aspect = base.w / base.h;
  const { rect } = L.resize(base, "se", 90, 10, [], stage, { aspect });
  assert.ok(Math.abs(rect.w / rect.h - aspect) < 0.01);
  assert.equal(rect.x, base.x);
  assert.equal(rect.y, base.y);
});

test("a locked shape keeps its proportions from a side", () => {
  const aspect = base.w / base.h;
  const east = L.resize(base, "e", 80, 0, [], stage, { aspect }).rect;
  assert.ok(Math.abs(east.w / east.h - aspect) < 0.01);
  const south = L.resize(base, "s", 0, 50, [], stage, { aspect }).rect;
  assert.ok(Math.abs(south.w / south.h - aspect) < 0.01);
  const north = L.resize(base, "n", 0, -40, [], stage, { aspect }).rect;
  assert.ok(Math.abs(north.w / north.h - aspect) < 0.01);
  assert.equal(north.y + north.h, base.y + base.h, "the bottom edge stays put when the top is dragged");
});

test("a free resize changes one dimension only", () => {
  const { rect } = L.resize(base, "e", 70, 0, [], stage, {});
  assert.equal(rect.h, base.h);
  assert.equal(rect.w, base.w + 70);
});

test("the east edge snaps to the stage margin", () => {
  const { rect, guides } = L.resize({ x: 700, y: 150, w: 400, h: 225 }, "e", 84, 0, [], stage, {});
  assert.equal(rect.x + rect.w, stage.w - L.PAD);
  assert.ok(guides.length >= 1);
});

test("a tile takes the width of another when it comes close", () => {
  const other = { x: 700, y: 400, w: 330, h: 200 };
  const { rect } = L.resize({ x: 100, y: 100, w: 300, h: 170 }, "e", 26, 0, [other], stage, {});
  assert.equal(rect.w, 330);
});

test("a resize never leaves the stage", () => {
  const { rect } = L.resize({ x: 900, y: 500, w: 250, h: 150 }, "se", 900, 900, [], stage, { aspect: 250 / 150 });
  assert.ok(inside(rect));
});
