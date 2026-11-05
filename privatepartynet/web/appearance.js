/*
 * How every window looks to this person: their accent color, text size and spacing (Settings, Appearance).
 *
 * The accent is one color the person picks. From it come, for the light and the dark theme each, the button color and its
 * hover, the text on it (white or near-black, whichever reads better), the accent used as text on the page (darkened or
 * lightened until it reads at 4.5:1 against the panels), and the selection tint. So any choice stays readable.
 * Text size and spacing scale theme.css's type and spacing steps through --text-scale and --space-scale.
 *
 * The functions are plain so tests can run them in Node; apply() writes the result onto a page.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PPNAppearance = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DEFAULT_ACCENT = "#2563eb"; // the blue of the icon's screen
  var PALETTE = ["#2563eb", "#0ea5e9", "#14b8a6", "#16a34a", "#ca8a04", "#ea580c", "#dc2626", "#db2777", "#9333ea", "#64748b"];
  var DENSITY = { compact: 0.8, "default": 1, spacious: 1.2 };
  // the panels the accent must read against, per theme (theme.css --surface and --bg)
  var SURFACES = { light: ["#ffffff", "#f2f4fa"], dark: ["#181f35", "#11172a"] };
  var DARK_TEXT = "#101420";

  function parseHex(hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    if (!m) return null;
    var n = parseInt(m[1], 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }

  function toHex(c) {
    function part(v) { var s = Math.max(0, Math.min(255, Math.round(v))).toString(16); return s.length === 1 ? "0" + s : s; }
    return "#" + part(c.r) + part(c.g) + part(c.b);
  }

  function luminance(c) {
    function lin(v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
    return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  }

  function contrast(a, b) {
    var x = luminance(typeof a === "string" ? parseHex(a) : a);
    var y = luminance(typeof b === "string" ? parseHex(b) : b);
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  }

  function mix(a, b, t) {
    return { r: a.r + (b.r - a.r) * t, g: a.g + (b.g - a.g) * t, b: a.b + (b.b - a.b) * t };
  }

  var WHITE = { r: 255, g: 255, b: 255 };
  var BLACK = { r: 0, g: 0, b: 0 };

  // Move a color toward black or white, a little at a time, until it reads at `target` against every one of `against`.
  function readableOn(color, against, target, toward) {
    var c = color;
    for (var i = 0; i < 40; i += 1) {
      if (against.every(function (bg) { return contrast(c, bg) >= target; })) return c;
      c = mix(c, toward, 0.06);
    }
    return c;
  }

  // Everything the page needs from one accent, for one theme ("light" or "dark").
  function derive(accentHex, scheme) {
    var base = parseHex(accentHex) || parseHex(DEFAULT_ACCENT);
    var surfaces = SURFACES[scheme === "dark" ? "dark" : "light"];
    var lightBg = scheme !== "dark";
    // the button: white text when it reads (at least 4.5:1), else near-black text
    var whiteOk = contrast(base, WHITE) >= 4.5;
    var text = whiteOk ? "#ffffff" : DARK_TEXT;
    var hover = mix(base, whiteOk ? BLACK : WHITE, lightBg ? 0.14 : 0.12);
    // the accent as text or an icon on the panels
    var ink = readableOn(base, surfaces, 4.5, lightBg ? BLACK : WHITE);
    // a tint behind a selected row
    var selected = mix(parseHex(surfaces[0]), base, lightBg ? 0.16 : 0.32);
    return { accent: toHex(base), hover: toHex(hover), text: text, ink: toHex(ink), selected: toHex(selected) };
  }

  function clampScale(value) {
    var n = Math.round(Number(value));
    return isFinite(n) ? Math.max(90, Math.min(130, n)) : 100;
  }

  // The CSS variables for one appearance: { "--accent": "light-dark(#.., #..)", ..., "--text-scale": "1.1" }.
  function variables(appearance) {
    var a = appearance || {};
    var light = derive(a.accent || DEFAULT_ACCENT, "light");
    var dark = derive(a.accent || DEFAULT_ACCENT, "dark");
    function pair(key) { return "light-dark(" + light[key] + ", " + dark[key] + ")"; }
    return {
      "--accent": pair("accent"),
      "--accent-hover": pair("hover"),
      "--accent-text": pair("text"),
      "--accent-ink": pair("ink"),
      "--selected": pair("selected"),
      "--text-scale": String(clampScale(a.text_scale) / 100),
      "--space-scale": String(DENSITY[a.density] || 1),
    };
  }

  function apply(doc, appearance) {
    var style = doc.documentElement.style;
    var vars = variables(appearance);
    Object.keys(vars).forEach(function (name) { style.setProperty(name, vars[name]); });
    doc.documentElement.dataset.density = (appearance && DENSITY[appearance.density]) ? appearance.density : "default";
  }

  return { DEFAULT_ACCENT: DEFAULT_ACCENT, PALETTE: PALETTE, DENSITY: DENSITY, parseHex: parseHex, toHex: toHex,
           contrast: contrast, derive: derive, variables: variables, apply: apply, clampScale: clampScale };
});
