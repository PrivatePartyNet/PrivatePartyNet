import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const U = require("../../privatepartynet/web/updates.js");

test("the About page keeps asking while a check or a download is under way", () => {
  assert.equal(U.busy({ state: "checking" }), true);
  assert.equal(U.busy({ state: "downloading", message: "Downloading PrivatePartyNet 3.2.1... 40%" }), true);
  for (const state of ["ready", "error", "current", "idle", "available"]) assert.equal(U.busy({ state }), false, state);
  assert.equal(U.busy(null), false);
  assert.equal(U.about({ state: "downloading", message: "Downloading PrivatePartyNet 3.2.1... 40%" }, 0).status,
    "Downloading PrivatePartyNet 3.2.1... 40%");
});

test("a checked update offers Restart now and Later", () => {
  const card = U.card({ state: "ready", version: "3.2.1", notes: "Fixes.", waiting: "" });
  assert.equal(card.show, true);
  assert.equal(card.text, "Ready: Restart now, or PrivatePartyNet 3.2.1 installs when you close PrivatePartyNet.");
  assert.deepEqual(card.buttons.map((b) => b.label), ["Restart now", "Later"]);
  assert.equal(card.notes, "Fixes.");
});

test("while a party, a call or the game network is on, Restart now waits and says why", () => {
  const card = U.card({ state: "ready", version: "3.2.1", waiting: "the game network" });
  assert.equal(card.text, "PrivatePartyNet 3.2.1 is ready. Restart now works once the game network ends, or it " +
    "installs when you close PrivatePartyNet.");
  assert.doesNotMatch(card.text, /installs when the game network ends/, "nothing installs by itself when it ends");
  assert.equal(card.buttons[0].disabled, true);
});

test("with automatic updates off, a new version offers Update, Later and Skip this version", () => {
  const card = U.card({ state: "available", version: "3.2.1", kind: "installed" });
  assert.equal(card.text, "PrivatePartyNet 3.2.1 is out.");
  assert.deepEqual(card.buttons.map((b) => b.action), ["restart", "later", "skip"]);
  assert.deepEqual(card.buttons.map((b) => b.label), ["Update", "Later", "Skip this version"]);
});

test("nothing shows when there is nothing to offer", () => {
  for (const state of ["idle", "current", "checking", "error", "dismissed", "skipped"]) {
    assert.equal(U.card({ state, version: "3.2.1" }).show, false, state);
  }
  assert.equal(U.card(null).show, false);
});

test("the About page says when it last checked and what it found", () => {
  const now = 1_800_000_000_000;
  assert.deepEqual(U.about({ automatic: true, message: "", checked_at: null }, now),
    { status: "Automatic updates are on.", checked: "Not checked yet." });
  const lines = U.about({ automatic: false, message: "PrivatePartyNet 3.2.0 is the newest version.", checked_at: now / 1000 - 120 }, now);
  assert.equal(lines.status, "PrivatePartyNet 3.2.0 is the newest version.");
  assert.equal(lines.checked, "Last checked 2 minutes ago.");
});
