/*
 * The control panel's page switching and the small badges on its rail. It only shows and hides pages and reads what
 * control.js already put on the page; it never talks to the app and holds no state of the party.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PPNNav = api;
    if (root.document) {
      var started = api.start(root.document, root);
      api.go = started.show; // the one window's rail opens Settings and Join here
    }
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var PAGES = ["home", "join", "nearby", "game", "messages", "chats", "settings"];
  var REMEMBER = "ppn.page";

  // "3 unread" -> 3, "" -> 0. The control panel writes the direct messages count as text.
  function numberIn(text) {
    var match = /\d+/.exec(String(text || ""));
    return match ? Number(match[0]) : 0;
  }

  function pageOrHome(name) {
    return PAGES.indexOf(name) >= 0 ? name : "home";
  }

  function start(doc, win) {
    var items = Array.from(doc.querySelectorAll(".navitem[data-go]"));
    var pages = Array.from(doc.querySelectorAll(".page"));
    var current = "";

    function show(name) {
      name = pageOrHome(name);
      current = name;
      pages.forEach(function (page) { page.hidden = page.dataset.page !== name; });
      items.forEach(function (item) {
        if (item.dataset.go === name) item.setAttribute("aria-current", "page");
        else item.removeAttribute("aria-current");
      });
      var pagesBox = doc.getElementById("pages");
      if (pagesBox) pagesBox.scrollTop = 0;
      try { win.localStorage.setItem(REMEMBER, name); } catch (err) { /* private window: nothing to remember */ }
      refresh();
    }

    items.forEach(function (item, index) {
      item.addEventListener("click", function () { show(item.dataset.go); });
      item.addEventListener("keydown", function (event) {
        if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
        var next = items[(index + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length];
        next.focus();
        event.preventDefault();
      });
    });

    // --- the badges ---------------------------------------------------------------------------------------

    function badge(id, count, hideWhenOpenPage) {
      var node = doc.getElementById(id);
      if (!node) return;
      var show = count > 0 && hideWhenOpenPage !== current;
      node.hidden = !show;
      node.textContent = show ? (count > 99 ? "99+" : String(count)) : "";
    }

    var lastRequests = 0;
    var toastTimer = null;

    function toast(text) {
      var box = doc.getElementById("ppnToast");
      if (!box) {
        box = doc.createElement("div");
        box.id = "ppnToast";
        box.className = "toast";
        box.setAttribute("role", "status");
        doc.body.append(box);
      }
      box.textContent = text;
      box.hidden = false;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(function () { box.hidden = true; }, 5000);
    }

    function refresh() {
      var requests = doc.querySelectorAll("#requestList li");
      badge("badgeHome", requests.length);
      if (requests.length > lastRequests && current !== "home") {
        var span = requests[requests.length - 1].querySelector("span");
        toast(span && span.textContent ? span.textContent : "Someone wants to join your party.");
      }
      lastRequests = requests.length;
      var unread = doc.getElementById("dmUnread");
      badge("badgeMessages", numberIn(unread && unread.textContent), "messages");
      badge("badgeNearby", doc.querySelectorAll("#peers tbody tr").length, "nearby");
      var light = doc.getElementById("lightGame");
      if (light) light.hidden = !doc.querySelector("#gameList .light.on");
    }

    if (win.MutationObserver) {
      var watch = new win.MutationObserver(refresh);
      ["requestList", "dmUnread", "gameList"].forEach(function (id) {
        var node = doc.getElementById(id);
        if (node) watch.observe(node, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["class"] });
      });
      var peers = doc.querySelector("#peers tbody");
      if (peers) watch.observe(peers, { childList: true });
    }

    // This window takes no files. Without this, a file dropped on it would replace the control panel with the file.
    ["dragover", "drop"].forEach(function (type) {
      doc.addEventListener(type, function (event) {
        if (event.dataTransfer && Array.prototype.indexOf.call(event.dataTransfer.types, "Files") >= 0) {
          event.preventDefault();
          event.dataTransfer.dropEffect = "none";
        }
      });
    });

    // The page list can fold down to its icons; the choice is remembered.
    var railToggle = doc.getElementById("railToggle");
    function setCompact(on) {
      doc.documentElement.classList.toggle("rail-compact", on);
      if (!railToggle) return;
      railToggle.setAttribute("aria-pressed", String(on));
      var label = on ? "Show names" : "Icons only";
      railToggle.title = label;
      railToggle.setAttribute("aria-label", label);
      var text = railToggle.querySelector("span");
      if (text) text.textContent = label;
    }
    var compact = false;
    try { compact = win.localStorage.getItem("ppn.rail") === "compact"; } catch (err) { /* nothing remembered */ }
    setCompact(compact);
    if (railToggle) {
      railToggle.addEventListener("click", function () {
        var on = !doc.documentElement.classList.contains("rail-compact");
        setCompact(on);
        try { win.localStorage.setItem("ppn.rail", on ? "compact" : "full"); } catch (err) { /* forgotten */ }
      });
    }

    var saved = "home";
    try { saved = win.localStorage.getItem(REMEMBER) || "home"; } catch (err) { /* nothing remembered */ }
    show(saved);
    return { show: show, current: function () { return current; } };
  }

  return { PAGES: PAGES, numberIn: numberIn, pageOrHome: pageOrHome, start: start };
});
