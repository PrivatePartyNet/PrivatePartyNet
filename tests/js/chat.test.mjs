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

function fakeStorage(initial = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    removeItem: (k) => { delete data[k]; },
  };
}

test("muted channels are kept per server and come back", () => {
  const storage = fakeStorage();
  Chat.writeMuted(storage, "srvA", new Set(["2", "5"]));
  assert.deepEqual([...Chat.readMuted(storage, "srvA")].sort(), ["2", "5"]);
  assert.equal(Chat.readMuted(storage, "srvB").size, 0, "another server's mutes are its own");
  Chat.writeMuted(storage, "srvA", new Set());
  assert.equal(Object.keys(storage.data).length, 0, "unmuting the last channel leaves nothing behind");
});

test("damaged, missing or blocked storage means nothing is muted", () => {
  for (const value of ["{not json", "42", '{"a":1}', "null"]) {
    assert.equal(Chat.readMuted(fakeStorage({ "ppn.muted.s": value }), "s").size, 0, value);
  }
  assert.deepEqual([...Chat.readMuted(fakeStorage({ "ppn.muted.s": '[3, "4", {"x":1}, null]' }), "s")], ["3", "4"]);
  assert.equal(Chat.readMuted(null, "s").size, 0);
  const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() {} };
  assert.equal(Chat.readMuted(blocked, "s").size, 0);
  Chat.writeMuted(blocked, "s", new Set(["1"]));
});

test("a muted channel shows no unread count; others show theirs", () => {
  const text = { id: 2, kind: "text" };
  assert.equal(Chat.showsUnread(text, 3, 1, false), true);
  assert.equal(Chat.showsUnread(text, 3, 1, true), false, "muted");
  assert.equal(Chat.showsUnread(text, 3, 2, false), false, "the open channel");
  assert.equal(Chat.showsUnread(text, 0, 1, false), false, "nothing unread");
  assert.equal(Chat.showsUnread({ id: 4, kind: "voice" }, 3, 1, false), false, "voice channels have no count");
});
