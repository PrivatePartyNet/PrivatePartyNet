import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const A = require("../../privatepartynet/web/appearance.js");

const SURFACES = { light: ["#ffffff", "#f2f4fa"], dark: ["#181f35", "#11172a"] };
const samples = [...A.PALETTE, "#ffffff", "#000000", "#ffff00", "#00ff00", "#7f7f7f", "#123456", "#fedcba"];

test("the default accent is the blue of the icon", () => {
  assert.equal(A.DEFAULT_ACCENT, "#2563eb");
  assert.equal(A.derive(null, "light").accent, "#2563eb");
});

test("any accent keeps its button text readable, in both themes", () => {
  for (const color of samples) {
    for (const scheme of ["light", "dark"]) {
      const d = A.derive(color, scheme);
      assert.ok(A.contrast(d.text, d.accent) >= 4.5 || A.contrast("#101420", d.accent) >= 4.5,
        `${color} ${scheme}: text ${d.text} on ${d.accent} is ${A.contrast(d.text, d.accent).toFixed(2)}`);
      // whichever text color was chosen is the better of the two
      const other = d.text === "#ffffff" ? "#101420" : "#ffffff";
      assert.ok(A.contrast(d.text, d.accent) >= 4.5 || A.contrast(d.text, d.accent) >= A.contrast(other, d.accent));
    }
  }
});

test("the accent used as text reads at 4.5:1 on the panels, whatever was picked", () => {
  for (const color of samples) {
    for (const scheme of ["light", "dark"]) {
      const ink = A.derive(color, scheme).ink;
      for (const bg of SURFACES[scheme]) {
        assert.ok(A.contrast(ink, bg) >= 4.5, `${color} ${scheme}: ink ${ink} on ${bg} is ${A.contrast(ink, bg).toFixed(2)}`);
      }
    }
  }
});

test("a bad color falls back to the default, and sizes stay within their limits", () => {
  assert.equal(A.derive("not a color", "dark").accent, "#2563eb");
  assert.equal(A.parseHex("#12345"), null);
  assert.deepEqual(A.parseHex("#ff8000"), { r: 255, g: 128, b: 0 });
  assert.equal(A.clampScale(200), 130);
  assert.equal(A.clampScale(10), 90);
  assert.equal(A.clampScale("x"), 100);
});

test("the variables carry both themes and the two scales", () => {
  const v = A.variables({ accent: "#16a34a", text_scale: 115, density: "compact" });
  assert.match(v["--accent"], /^light-dark\(#[0-9a-f]{6}, #[0-9a-f]{6}\)$/);
  assert.equal(v["--text-scale"], "1.15");
  assert.equal(v["--space-scale"], "0.8");
  assert.equal(A.variables({})["--space-scale"], "1");
});
