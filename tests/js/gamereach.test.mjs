import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const R = require("../../privatepartynet/web/gamereach.js");

const answer = {
  ok: true,
  groups: [
    { id: "sharing", label: "File and printer sharing", detail: "Members could open shared folders.", open: false },
    { id: "remote_desktop", label: "Remote Desktop", detail: "Members could ask to sign in.", open: true },
  ],
};

// Just enough of a document for render(): elements that hold children, text and listeners.
function element(tag, doc) {
  return {
    tagName: tag, ownerDocument: doc, children: [], dataset: {}, listeners: {}, className: "", checked: false,
    set textContent(value) { this._text = value; if (value === "") this.children = []; },
    get textContent() { return (this._text || "") + this.children.map((c) => c.textContent).join(""); },
    append(...items) { this.children.push(...items); },
    addEventListener(name, fn) { this.listeners[name] = fn; },
  };
}

function fakeList() {
  const doc = { createElement: (tag) => element(tag, doc) };
  return element("div", doc);
}

test("each group is a row, closed unless the app says it is open", () => {
  assert.deepEqual(R.rows(answer), [
    { id: "sharing", label: "File and printer sharing", detail: "Members could open shared folders.", open: false },
    { id: "remote_desktop", label: "Remote Desktop", detail: "Members could ask to sign in.", open: true },
  ]);
});

test("a malformed answer gives no rows rather than a broken menu", () => {
  assert.deepEqual(R.rows(null), []);
  assert.deepEqual(R.rows({ groups: "sharing" }), []);
  assert.deepEqual(R.rows({ groups: [null, { id: 3 }, { id: "x", label: "X", open: "yes" }] }), [
    { id: "x", label: "X", detail: "", open: false },
  ]);
});

test("the menu shows a ticked box for each open group and says what it opens", () => {
  const list = fakeList();
  R.render(list, answer, () => {});
  assert.equal(list.children.length, 2);
  const [sharing, desktop] = list.children.map((label) => label.children[0]);
  assert.equal(sharing.checked, false);
  assert.equal(desktop.checked, true);
  assert.equal(sharing.dataset.group, "sharing");
  assert.match(list.textContent, /File and printer sharing.*Members could open shared folders/);
});

test("ticking a box asks for that group to be opened, and unticking to close it", () => {
  const list = fakeList();
  const asked = [];
  R.render(list, answer, (group, open) => asked.push([group, open]));
  const [sharing, desktop] = list.children.map((label) => label.children[0]);
  sharing.checked = true;
  sharing.listeners.change();
  desktop.checked = false;
  desktop.listeners.change();
  assert.deepEqual(asked, [["sharing", true], ["remote_desktop", false]]);
});

test("drawing it again replaces the rows instead of adding to them", () => {
  const list = fakeList();
  R.render(list, answer, () => {});
  R.render(list, answer, () => {});
  assert.equal(list.children.length, 2);
});
