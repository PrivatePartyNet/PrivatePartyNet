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

test("a download shows a bar with its share, and the words say how much, how fast and how long", () => {
  const view = { state: "downloading", version: "3.5.3",
    message: "Downloading PrivatePartyNet 3.5.3 — 227 / 391 MB · 12.4 MB/s · about 15 s left",
    download: { done: 227_400_000, total: 391_200_000, speed: 12_400_000, eta: 15 } };
  const shown = U.progress(view);
  assert.equal(shown.show, true);
  assert.equal(shown.text, view.message);
  assert.ok(Math.abs(shown.bar - 227.4 / 391.2) < 1e-9);
  const card = U.card(view);
  assert.equal(card.show, true, "the banner over every page shows it");
  assert.equal(card.bar, shown.bar);
  assert.deepEqual(card.buttons, []);
  assert.equal(U.busy(view), true);
});

test("the bar stays within its track whatever the view says", () => {
  assert.equal(U.progress({ state: "downloading", download: { done: 900, total: 600 } }).bar, 1);
  assert.equal(U.progress({ state: "downloading", download: { done: -5, total: 600 } }).bar, 0);
  assert.equal(U.progress({ state: "downloading", progress: 0.25 }).bar, 0.25, "an older view's share");
  assert.equal(U.progress({ state: "downloading", download: "nonsense" }).bar, 0);
});

test("while the file is checked against GitHub's SHA-256 the words say so, with no bar", () => {
  const view = { state: "verifying", message: "Checking the download..." };
  assert.deepEqual(U.progress(view), { show: true, text: "Checking the download...", bar: null });
  assert.equal(U.card(view).text, "Checking the download...");
  assert.equal(U.busy(view), true, "the pages keep asking until it is ready");
  for (const state of ["ready", "idle", "current", "error"]) assert.equal(U.progress({ state }).show, false, state);
});

test("Settings > Updates says what the updater is doing whether or not the button was pressed", () => {
  const now = Date.now() / 1000;
  const downloading = U.status({ state: "downloading", message: "Downloading PrivatePartyNet 3.5.3 — 1 / 391 MB",
    download: { done: 1_000_000, total: 391_000_000 }, checked_at: now - 60 });
  assert.equal(downloading.text, "Downloading PrivatePartyNet 3.5.3 — 1 / 391 MB");
  assert.ok(downloading.bar > 0 && downloading.bar < 0.01);
  assert.equal(downloading.checked, "Last checked 1 minute ago.");
  const ready = U.status({ state: "ready", message: "Ready: Restart now, or PrivatePartyNet 3.5.3 installs when you close PrivatePartyNet." });
  assert.equal(ready.text, "Ready: Restart now, or PrivatePartyNet 3.5.3 installs when you close PrivatePartyNet.");
  assert.equal(ready.bar, null);
  assert.equal(U.status({ state: "current", message: "PrivatePartyNet 3.5.3 is the newest version." }).text,
    "PrivatePartyNet 3.5.3 is the newest version.");
  assert.equal(U.status({ state: "idle", message: "" }).text, "");
  assert.equal(U.status({ state: "dismissed", message: "PrivatePartyNet 3.5.3 is out." }).text, "", "set aside with Later");
  assert.deepEqual(U.status(null), { text: "", bar: null, checked: "" });
});
