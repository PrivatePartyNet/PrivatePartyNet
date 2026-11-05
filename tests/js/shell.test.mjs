import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Shell = require("../../privatepartynet/web/appshell.js");
const Bridge = require("../../privatepartynet/web/roombridge.js");

test("a server's rail button shows its initials", () => {
  assert.equal(Shell.initials("Heroes night"), "HN");
  assert.equal(Shell.initials("kay"), "KA");
  assert.equal(Shell.initials("  Ñandú   party  "), "ÑP");
  assert.equal(Shell.initials("!!!"), "?");
  assert.equal(Shell.initials(""), "?");
});

test("a room is told it is inside the one window, where the shell is, and the theme", () => {
  const url = new URL(Shell.frameUrl("https://192.168.1.20:8765/join?token=abc&feed=x", "http://127.0.0.1:5000", "dark"));
  assert.equal(url.searchParams.get("token"), "abc");
  assert.equal(url.searchParams.get("feed"), "x");
  assert.equal(url.searchParams.get("embedded"), "1");
  assert.equal(url.searchParams.get("shell"), "http://127.0.0.1:5000");
  assert.equal(url.searchParams.get("theme"), "dark");
});

test("a reload goes to the room itself, since a join link works once", () => {
  assert.equal(Shell.reloadUrl("https://10.0.0.2:8765/join?token=abc&feed=x"), "https://10.0.0.2:8765/?feed=x");
  assert.equal(Shell.reloadUrl("http://127.0.0.1:9000/host"), "http://127.0.0.1:9000/host");
});

test("what a room reports is cleaned before the rail shows it", () => {
  assert.deepEqual(Shell.cleanState({ unread: 3.7, requests: -2, voice: { channel: "Voice 1", muted: 1 } }),
                   { unread: 3, requests: 0, voice: { channel: "Voice 1", muted: true, deafened: false } });
  assert.deepEqual(Shell.cleanState({ unread: "9", requests: Infinity, voice: "yes" }), { unread: 0, requests: 0, voice: null });
  assert.equal(Shell.cleanState({ unread: 1e9 }).unread, 9999);
  assert.equal(Shell.badgeText(150), "99+");
});

test("the shell passes on exactly the calls the room's bridge offers", () => {
  assert.deepEqual([...Shell.ROOM_CALLS].sort(), [...Bridge.METHODS].sort());
  assert.match(Shell.FRAME_ALLOW, /\bmicrophone\b/);
});

test("a room talks only to a shell on this PC's loopback address", () => {
  assert.equal(Bridge.shellOrigin("?embedded=1&shell=http%3A%2F%2F127.0.0.1%3A5000"), "http://127.0.0.1:5000");
  for (const search of ["?shell=http%3A%2F%2F127.0.0.1%3A5000", "?embedded=1&shell=https%3A%2F%2F127.0.0.1%3A5000",
                        "?embedded=1&shell=http%3A%2F%2Fevil.example%3A5000", "?embedded=1&shell=http%3A%2F%2F127.0.0.1%3A5000.evil.example",
                        "?embedded=1&shell=http%3A%2F%2F127.0.0.1%3A5000%2Fx", "?embedded=1", ""]) {
    assert.equal(Bridge.shellOrigin(search), "", search);
  }
});
