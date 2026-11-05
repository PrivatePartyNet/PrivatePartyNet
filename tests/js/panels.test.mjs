import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Panels = require("../../privatepartynet/web/panels.js");

test("a panel width stays between its limits", () => {
  assert.equal(Panels.clampWidth(300, 200, 440), 300);
  assert.equal(Panels.clampWidth(900, 200, 440), 440);
  assert.equal(Panels.clampWidth(120, 200, 440), 200);
  assert.equal(Panels.clampWidth(250.6, 200, 440), 251);
});

test("a width that is not a number is ignored", () => {
  assert.equal(Panels.clampWidth("wide", 200, 440), null);
  assert.equal(Panels.clampWidth(undefined, 200, 440), null);
});

test("remembered widths and folds survive a page without storage", () => {
  assert.equal(Panels.read(null, "x"), null);
  Panels.write(null, "x", 5); // nothing to write to: no error
  const box = new Map();
  const storage = { getItem: (k) => (box.has(k) ? box.get(k) : null), setItem: (k, v) => box.set(k, v), removeItem: (k) => box.delete(k) };
  Panels.write(storage, "room.sidebar", 320);
  assert.equal(Panels.read(storage, "room.sidebar"), "320");
  Panels.write(storage, "room.sidebar", null);
  assert.equal(Panels.read(storage, "room.sidebar"), null);
});
