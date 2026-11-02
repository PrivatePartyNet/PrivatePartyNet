import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Chat = require("../../privatepartynet/web/chat.js");

test("nothing in the page can shrink a picture", () => {
  assert.equal(Chat.fitSize, undefined);
  assert.equal(Chat.MAX_SIDE, undefined);
});

test("a PNG is saved as a lossless PNG first and lossy only if that does not fit", () => {
  const plan = Chat.encodingPlan("image/png");
  assert.deepEqual(plan[0], ["image/png", undefined]);
  assert.equal(plan[1][0], "image/webp");
  assert.ok(plan.length > 3);
});

test("a JPEG stays a JPEG and a WebP stays a WebP, with the quality stepping down", () => {
  const jpeg = Chat.encodingPlan("image/jpeg");
  assert.ok(jpeg.every((step) => step[0] === "image/jpeg"));
  const webp = Chat.encodingPlan("image/webp");
  assert.ok(webp.every((step) => step[0] === "image/webp"));
  for (const plan of [jpeg, webp]) {
    const qualities = plan.map((step) => step[1]);
    assert.deepEqual(qualities, [...qualities].sort((a, b) => b - a));
    assert.ok(qualities[0] >= 0.9);
  }
});

test("the size limit matches the server's", () => {
  assert.equal(Chat.MAX_BYTES, 12 * 1024 * 1024);
});
