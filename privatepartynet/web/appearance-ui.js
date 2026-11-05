/*
 * The Appearance card in Settings: accent color (swatches, R G B sliders, a hex box), text size and spacing. A change shows
 * at once (appearance.js) and is saved on this PC a moment later (set_appearance in app.py). Nothing here is sent anywhere.
 */
(function () {
  "use strict";

  var A = window.PPNAppearance;
  if (!A) return;
  var $ = function (id) { return document.getElementById(id); };
  var el = {
    swatches: $("accentSwatches"), r: $("accentR"), g: $("accentG"), b: $("accentB"), rv: $("accentRv"), gv: $("accentGv"),
    bv: $("accentBv"), hex: $("accentHex"), reset: $("accentReset"), scale: $("textScale"), scaleV: $("textScaleV"),
    density: $("density"),
  };
  if (!el.swatches) return;

  var current = { accent: null, text_scale: 100, density: "default" };
  var saveTimer = null;

  function accentHex() {
    return current.accent || A.DEFAULT_ACCENT;
  }

  function show() {
    var hex = accentHex();
    var c = A.parseHex(hex);
    el.r.value = c.r; el.g.value = c.g; el.b.value = c.b;
    el.rv.textContent = c.r; el.gv.textContent = c.g; el.bv.textContent = c.b;
    if (document.activeElement !== el.hex) el.hex.value = hex;
    el.swatches.querySelectorAll("button").forEach(function (b) {
      b.setAttribute("aria-checked", String(b.dataset.color === hex));
    });
    el.scale.value = current.text_scale;
    el.scaleV.textContent = current.text_scale + "%";
    el.density.querySelectorAll("button").forEach(function (b) {
      b.setAttribute("aria-checked", String(b.dataset.density === current.density));
    });
    A.apply(document, current);
  }

  function change(patch) {
    Object.keys(patch).forEach(function (k) { current[k] = patch[k]; });
    show();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      var api = window.pywebview && window.pywebview.api;
      if (api && api.set_appearance) api.set_appearance(current.accent, current.text_scale, current.density);
    }, 300);
  }

  A.PALETTE.forEach(function (color) {
    var b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.dataset.color = color;
    b.style.background = color;
    b.title = color;
    b.setAttribute("aria-label", "Accent " + color);
    b.addEventListener("click", function () { change({ accent: color === A.DEFAULT_ACCENT ? null : color }); });
    el.swatches.append(b);
  });

  [el.r, el.g, el.b].forEach(function (slider) {
    slider.addEventListener("input", function () {
      var hex = A.toHex({ r: Number(el.r.value), g: Number(el.g.value), b: Number(el.b.value) });
      change({ accent: hex === A.DEFAULT_ACCENT ? null : hex });
    });
  });
  el.hex.addEventListener("input", function () {
    var c = A.parseHex(el.hex.value);
    if (c) change({ accent: A.toHex(c) === A.DEFAULT_ACCENT ? null : A.toHex(c) });
  });
  el.hex.addEventListener("blur", show);
  el.reset.addEventListener("click", function () { change({ accent: null }); });
  el.scale.addEventListener("input", function () { change({ text_scale: A.clampScale(el.scale.value) }); });
  el.density.querySelectorAll("button").forEach(function (b) {
    b.addEventListener("click", function () { change({ density: b.dataset.density }); });
  });

  show();
  window.addEventListener("pywebviewready", function () {
    window.pywebview.api.state().then(function (state) {
      if (state && state.appearance) {
        current = { accent: state.appearance.accent || null, text_scale: A.clampScale(state.appearance.text_scale),
                    density: A.DENSITY[state.appearance.density] ? state.appearance.density : "default" };
        show();
      }
    }).catch(function () { /* the next visit to Settings shows what is saved */ });
  });
})();
