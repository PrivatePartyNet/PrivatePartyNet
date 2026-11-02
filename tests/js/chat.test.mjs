import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Chat = require("../../privatepartynet/web/chat.js");

test("a small picture is left at its own size", () => {
  assert.deepEqual(Chat.fitSize(800, 600, 1600), { w: 800, h: 600 });
  assert.deepEqual(Chat.fitSize(1600, 1600, 1600), { w: 1600, h: 1600 });
});

test("a big picture is shrunk so its longest side is the limit, in its own shape", () => {
  assert.deepEqual(Chat.fitSize(4000, 3000, 1600), { w: 1600, h: 1200 });
  assert.deepEqual(Chat.fitSize(3000, 6000, 1600), { w: 800, h: 1600 });
});

test("a very thin picture never shrinks to nothing", () => {
  const { w, h } = Chat.fitSize(20000, 3, 1600);
  assert.equal(w, 1600);
  assert.ok(h >= 1);
});

test("the size limits match the server's", () => {
  assert.equal(Chat.MAX_BYTES, 2 * 1024 * 1024);
  assert.equal(Chat.MAX_SIDE, 1600);
});
