/*
 * Panels that can be made wider or narrower, and sections that fold shut. Used by every PrivatePartyNet window.
 *
 *   PPNPanels.resizable(panel, { side: "right" | "left", variable: "--sidebar-w", min: 180, max: 420, key: "room.sidebar" })
 *     puts a handle on the panel's inner edge. Dragging it, or the arrow keys while it has focus, sets the CSS variable on
 *     <html> (the page's layout uses that variable for the panel's width). A double-click goes back to the page's own width.
 *     Widths are remembered per page.
 *   PPNPanels.collapsible(heading, body, { key: "room.channels", label: "Channels" })
 *     turns the heading into a button with a chevron that folds the body away, and remembers it.
 *
 * clampWidth() and the storage helpers are plain functions so tests can run them in Node.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PPNPanels = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var PREFIX = "ppn.panel.";
  var STEP = 16; // pixels per arrow key press; Shift moves four times as far

  function clampWidth(width, min, max) {
    var value = Math.round(Number(width));
    if (!isFinite(value)) return null;
    return Math.max(min, Math.min(max, value));
  }

  function read(storage, key) {
    try { return storage ? storage.getItem(PREFIX + key) : null; } catch (err) { return null; }
  }

  function write(storage, key, value) {
    try {
      if (!storage) return;
      if (value === null || value === undefined) storage.removeItem(PREFIX + key);
      else storage.setItem(PREFIX + key, String(value));
    } catch (err) { /* a page without storage forgets, and nothing else changes */ }
  }

  function resizable(panel, options) {
    if (!panel) return null;
    var doc = panel.ownerDocument;
    var win = doc.defaultView;
    var storage = win.localStorage;
    var rootStyle = doc.documentElement.style;
    var min = options.min || 160;
    var max = options.max || 600;
    var side = options.side === "left" ? "left" : "right"; // the edge the handle sits on

    var handle = doc.createElement("div");
    handle.className = "resize-handle " + side;
    handle.tabIndex = 0;
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    handle.setAttribute("aria-label", "Resize " + (options.label || "panel") + " (drag, or arrow keys; double-click resets)");
    handle.title = "Drag to resize. Double-click to reset.";
    panel.append(handle);
    if (win.getComputedStyle(panel).position === "static") panel.style.position = "relative";

    function current() {
      return panel.getBoundingClientRect().width;
    }

    function apply(width, remember) {
      var value = clampWidth(width, min, Math.min(max, Math.max(min, win.innerWidth - (options.leave || 360))));
      if (value === null) return;
      rootStyle.setProperty(options.variable, value + "px");
      handle.setAttribute("aria-valuenow", String(value));
      if (remember) write(storage, options.key, value);
    }

    function reset() {
      rootStyle.removeProperty(options.variable);
      write(storage, options.key, null);
    }

    var saved = read(storage, options.key);
    if (saved !== null) apply(saved, false);
    handle.setAttribute("aria-valuemin", String(min));
    handle.setAttribute("aria-valuemax", String(max));

    handle.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      event.preventDefault();
      var startX = event.clientX;
      var startWidth = current();
      doc.body.classList.add("resizing");
      // The moves are read on the document, as the screen tiles' drags are, so a drag that leaves the thin handle (or a
      // pointer the page cannot capture) still follows; capture is only a help where it works.
      try { handle.setPointerCapture(event.pointerId); } catch (err) { /* not capturable: the document still hears it */ }
      function move(e) {
        var delta = e.clientX - startX;
        apply(side === "right" ? startWidth + delta : startWidth - delta, false);
      }
      function up() {
        doc.removeEventListener("pointermove", move);
        doc.removeEventListener("pointerup", up);
        doc.removeEventListener("pointercancel", up);
        doc.body.classList.remove("resizing");
        apply(current(), true);
      }
      doc.addEventListener("pointermove", move);
      doc.addEventListener("pointerup", up);
      doc.addEventListener("pointercancel", up);
    });
    handle.addEventListener("dblclick", reset);
    handle.addEventListener("keydown", function (event) {
      var grow = side === "right" ? "ArrowRight" : "ArrowLeft";
      var shrink = side === "right" ? "ArrowLeft" : "ArrowRight";
      var step = event.shiftKey ? STEP * 4 : STEP;
      if (event.key === grow) apply(current() + step, true);
      else if (event.key === shrink) apply(current() - step, true);
      else if (event.key === "Home") apply(min, true);
      else if (event.key === "End") apply(max, true);
      else if (event.key === "Escape" || event.key === "Delete") reset();
      else return;
      event.preventDefault();
    });
    return { handle: handle, set: function (w) { apply(w, true); }, reset: reset };
  }

  function collapsible(heading, body, options) {
    if (!heading || !body) return null;
    var doc = heading.ownerDocument;
    var storage = doc.defaultView.localStorage;
    var button = doc.createElement("button");
    button.type = "button";
    button.className = "fold";
    button.setAttribute("aria-controls", body.id || "");
    button.title = (options.label ? options.label + ": " : "") + "fold or unfold";
    var chevron = doc.createElement("span");
    chevron.className = "chev";
    chevron.setAttribute("aria-hidden", "true");
    // the heading's own text moves into the button, so the whole heading is the target
    while (heading.firstChild && !(heading.firstChild.nodeType === 1 && heading.firstChild.classList.contains("keep"))) {
      button.append(heading.firstChild);
    }
    button.prepend(chevron);
    heading.prepend(button);
    heading.classList.add("foldable");

    function set(folded, remember) {
      body.hidden = folded;
      heading.classList.toggle("folded", folded);
      button.setAttribute("aria-expanded", String(!folded));
      if (remember) write(storage, options.key, folded ? "folded" : null);
    }
    set(read(storage, options.key) === "folded", false);
    button.addEventListener("click", function () { set(!body.hidden, true); });
    return { set: function (folded) { set(folded, true); }, folded: function () { return body.hidden; } };
  }

  return { clampWidth: clampWidth, resizable: resizable, collapsible: collapsible, read: read, write: write, PREFIX: PREFIX };
});
