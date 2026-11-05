/*
 * The icons of every PrivatePartyNet window, drawn by hand on a 24 by 24 grid and kept here as one set so no page needs an
 * icon file, a font or a download. Each is a stroke drawing; the page's CSS (svg.ic) sets the line width and color.
 *
 * In a page: <svg class="ic"><use href="#i-home"></use></svg>, or any element with data-icon="home" (the script fills it in).
 * From script: PPNIcons.node("home") returns an <svg>. The strings below are fixed; nothing from the network goes into them.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PPNIcons = api;
    api.install(root.document);
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var ICONS = {
    home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/>',
    server: '<path d="M12 3l9 4.5-9 4.5-9-4.5z"/><path d="M3 12l9 4.5 9-4.5"/><path d="M3 16.5L12 21l9-4.5"/>',
    hash: '<path d="M5 9h15M4 15h15M10 3L8 21M16 3l-2 18"/>',
    "bell-off": '<path d="M6 8a6 6 0 0112 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 003.4 0"/><path d="M3 3l18 18"/>',
    speaker: '<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16.5 8.5a5 5 0 010 7"/><path d="M19 6a8.5 8.5 0 010 12"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0"/><path d="M12 18v3M9 21h6"/>',
    "mic-off": '<path d="M3 3l18 18"/><path d="M9 9v2a3 3 0 005 2.1"/><path d="M15 9.3V6a3 3 0 00-5.6-1.5"/><path d="M5 11a7 7 0 0011.2 5.6M19 11a7 7 0 01-.6 2.8"/><path d="M12 18v3M9 21h6"/>',
    headphones: '<path d="M4 14v-2a8 8 0 0116 0v2"/><rect x="3" y="14" width="4" height="6" rx="1.5"/><rect x="17" y="14" width="4" height="6" rx="1.5"/>',
    "headphones-off": '<path d="M3 3l18 18"/><path d="M4 14v-2a8 8 0 012.4-5.7M18.2 8.5A8 8 0 0120 12v2"/><rect x="3" y="14" width="4" height="6" rx="1.5"/><rect x="17" y="14" width="4" height="6" rx="1.5"/>',
    screen: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    live: '<circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/><path d="M7.7 7.7a6 6 0 000 8.6M16.3 7.7a6 6 0 010 8.6"/><path d="M4.8 4.8a10 10 0 000 14.4M19.2 4.8a10 10 0 010 14.4"/>',
    chat: '<path d="M4 5h16v11H9l-5 4V5z"/>',
    picture: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="M21 16l-5-5-8 8"/>',
    send: '<path d="M21 3L10 14"/><path d="M21 3l-7 18-4-7-7-4z"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0116 0"/>',
    users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0113 0"/><circle cx="17" cy="9" r="2.8"/><path d="M16 14.2A5.5 5.5 0 0121.5 20"/>',
    gamepad: '<rect x="2.5" y="7" width="19" height="11" rx="5"/><path d="M7.5 10.5v4M5.5 12.5h4"/><circle cx="15.5" cy="11.5" r="0.9" fill="currentColor" stroke="none"/><circle cx="18" cy="13.5" r="0.9" fill="currentColor" stroke="none"/>',
    network: '<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><path d="M12 7.5v4M12 11.5L6.5 17M12 11.5l5.5 5.5"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>',
    key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M16 7l3 3M14 9l2 2"/>',
    link: '<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M4 16V6a2 2 0 012-2h10"/>',
    folder: '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>',
    gear: '<circle cx="12" cy="12" r="3.5"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><circle cx="12" cy="7.6" r="0.7" fill="currentColor" stroke="none"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    warn: '<path d="M12 3l10 18H2z"/><path d="M12 10v5"/><circle cx="12" cy="18" r="0.7" fill="currentColor" stroke="none"/>',
    ellipsis: '<circle cx="5" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.4" fill="currentColor" stroke="none"/>',
    pin: '<path d="M12 17v5M9 3h6l-1 6 3 3H7l3-3z"/>',
    fullscreen: '<path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/>',
    "pop-out": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>',
    pip: '<rect x="3" y="5" width="18" height="14" rx="2"/><rect x="12" y="12" width="6" height="4" rx="1" fill="currentColor" stroke="none"/>',
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "panel-right": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    "panel-left": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    sliders: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    edit: '<path d="M4 20h4L19 9l-4-4L4 16v4z"/>',
    leave: '<path d="M10 4H5v16h5"/><path d="M15 8l4 4-4 4M19 12H9"/>',
    shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
    wifi: '<path d="M2 9a15 15 0 0120 0M5.5 12.5a10 10 0 0113 0M9 16a5 5 0 016 0"/><circle cx="12" cy="19.5" r="0.9" fill="currentColor" stroke="none"/>',
    download: '<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
    upload: '<path d="M12 20V8M7 13l5-5 5 5M4 4h16"/>',
    "eye-off": '<path d="M3 3l18 18"/><path d="M10.6 6.1A10 10 0 0112 6c5 0 9 6 9 6a15 15 0 01-3 3.5M6.6 6.6A15 15 0 003 12s4 6 9 6a9 9 0 004.4-1.2"/><path d="M9.9 9.9a3 3 0 004.2 4.2"/>',
    cpu: '<rect x="7" y="7" width="10" height="10" rx="1"/><path d="M9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    arrowdown: '<path d="M12 4v14M6 12l6 6 6-6"/>',
    arrowup: '<path d="M12 20V6M6 12l6-6 6 6"/>',
    frames: '<rect x="3" y="5" width="14" height="12" rx="1"/><path d="M17 9l4-2v10l-4-2"/>',
    ruler: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 3v4M12 3v6M16 3v4"/>',
    hat: '<path d="M12 5l7.5 15h-15z"/><circle cx="12" cy="3.8" r="1.7" fill="currentColor" stroke="none"/><path d="M8.7 13.5h6.6M6.9 17.2h10.2"/>',
    moon: '<path d="M20 14.5A8 8 0 019.5 4 8 8 0 1020 14.5z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/>',
    monitor: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  };

  var SPRITE_ID = "ppn-icon-sprite";

  function names() {
    return Object.keys(ICONS);
  }

  function svgMarkup(name) {
    return '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + (ICONS[name] || "") + "</svg>";
  }

  // The sprite lets a page write <use href="#i-name"> and have the browser draw it from one copy.
  function spriteMarkup() {
    var symbols = names().map(function (name) {
      return '<symbol id="i-' + name + '" viewBox="0 0 24 24">' + ICONS[name] + "</symbol>";
    });
    return '<svg id="' + SPRITE_ID + '" xmlns="http://www.w3.org/2000/svg" width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">' +
      symbols.join("") + "</svg>";
  }

  function node(doc, name) {
    var box = doc.createElement("span");
    box.innerHTML = svgMarkup(name);
    return box.firstChild;
  }

  function fill(doc) {
    doc.querySelectorAll("[data-icon]").forEach(function (target) {
      if (target.querySelector("svg.ic")) return;
      target.insertAdjacentHTML("afterbegin", svgMarkup(target.getAttribute("data-icon")));
    });
  }

  function install(doc) {
    if (!doc || doc.getElementById(SPRITE_ID)) return;
    var add = function () {
      if (doc.getElementById(SPRITE_ID)) return;
      doc.body.insertAdjacentHTML("afterbegin", spriteMarkup());
      fill(doc);
    };
    if (doc.body) add();
    else doc.addEventListener("DOMContentLoaded", add);
  }

  return {
    names: names,
    markup: svgMarkup,
    sprite: spriteMarkup,
    node: function (name, doc) { return node(doc || document, name); },
    fill: function (doc) { fill(doc || document); },
    install: install,
  };
});
