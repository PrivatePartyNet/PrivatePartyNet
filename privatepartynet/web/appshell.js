/*
 * The one window (shell.html): the rail of servers on the far left, and beside it one view at a time. Home is the
 * control panel (control.html, from this same origin, so it uses this window's bridge to the app through
 * framebridge.js). Each server is its party's room in a frame from the party's own address; a room asks the app for
 * things through postMessage (roombridge.js), and this page passes on only the calls a party window may make
 * (ROOM_CALLS), only from that frame and that origin, keyed by the server, to Controller.room_call.
 *
 * Hidden frames stay loaded, so a call keeps going while another server is shown; the rail then shows the call's
 * controls at its foot. Which servers to show comes from the app (shell_state: open, close, info, refused, home).
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root && root.document && root.document.getElementById("servers")) {
    root.PPNShellParts = api;
    api.start(root);
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var ROOM_CALLS = ["open_viewer", "close_window", "set_fullscreen", "keep_chat", "keep_blob", "appearance"];
  var POLL_MS = 1000;
  var READY_MS = 25000;
  var FRAME_ALLOW = "microphone; fullscreen; clipboard-write; autoplay; display-capture";

  // "Heroes night" -> "HN"; "kay" -> "KA"; nothing usable -> "?"
  function initials(name) {
    var words = String(name || "").trim().split(/\s+/).filter(function (w) { return /[\p{L}\p{N}]/u.test(w); });
    if (!words.length) return "?";
    if (words.length === 1) return Array.from(words[0]).slice(0, 2).join("").toUpperCase();
    return (Array.from(words[0])[0] + Array.from(words[1])[0]).toUpperCase();
  }

  function hueOf(text) {
    var h = 0;
    for (var i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) % 360;
    return h;
  }

  function originOf(url) {
    try {
      return new URL(url).origin;
    } catch (err) {
      return "";
    }
  }

  // The room's address inside this window: it is told it is embedded, where the shell is (the only page it will talk
  // to), and which theme this person uses.
  function frameUrl(url, shellOrigin, theme) {
    var u = new URL(url);
    u.searchParams.set("embedded", "1");
    u.searchParams.set("shell", shellOrigin);
    if (theme) u.searchParams.set("theme", theme);
    return u.toString();
  }

  // The address to load again once signed in: a join link works once, so a reload goes to the room itself.
  function reloadUrl(url) {
    var u = new URL(url);
    if (u.pathname === "/join") {
      u.pathname = "/";
      u.searchParams.delete("token");
    }
    return u.toString();
  }

  function badgeText(n) {
    return n > 99 ? "99+" : String(n);
  }

  // A room's report, cleaned: counts are whole numbers from 0, the call is a channel name and two flags, or null.
  function cleanState(msg) {
    function count(v) { return typeof v === "number" && isFinite(v) && v > 0 ? Math.min(Math.floor(v), 9999) : 0; }
    var voice = null;
    if (msg && msg.voice && typeof msg.voice === "object") {
      voice = { channel: String(msg.voice.channel || "").slice(0, 64), muted: Boolean(msg.voice.muted),
                deafened: Boolean(msg.voice.deafened) };
    }
    return { unread: count(msg && msg.unread), requests: count(msg && msg.requests), voice: voice };
  }

  function start(win) {
    var doc = win.document;
    var $ = function (id) { return doc.getElementById(id); };
    var el = {
      shell: $("shell"), list: $("serverList"), views: $("views"), home: $("homeBtn"), homeFrame: $("view-home"),
      homeItem: doc.querySelector('.srv-item[data-key="home"]'), add: $("addBtn"), settings: $("settingsBtn"),
      dock: $("voiceDock"), dockServer: $("dockServer"), dockMute: $("dockMute"), dockDeafen: $("dockDeafen"),
      dockLeave: $("dockLeave"), overlay: $("overlay"), overlayTitle: $("overlayTitle"), overlayFrame: $("overlayFrame"),
      overlayClose: $("overlayClose"), tip: $("railTip"), menu: $("railMenu"),
    };
    el.homeBadge = el.home.querySelector(".srv-badge");
    doc.documentElement.setAttribute("data-ppn-shell", "");

    var servers = new Map(); // key -> { key, name, url, origin, own, frame, item, button, badge, voiceMark, state, ready }
    var current = "home";
    var theme = doc.documentElement.dataset.theme || "dark";
    var look = null;
    var polling = null;

    function api() {
      return win.pywebview && win.pywebview.api;
    }

    function icon(name) {
      return win.PPNIcons ? win.PPNIcons.node(name) : doc.createElement("span");
    }

    // --- the rail -----------------------------------------------------------------------------------------------

    function addServer(info) {
      if (!info || typeof info.key !== "string" || typeof info.url !== "string") return;
      if (servers.has(info.key)) {
        if (info.select !== false) show(info.key);
        return;
      }
      var name = String(info.name || "Party").slice(0, 60);
      var item = doc.createElement("div");
      item.className = "srv-item";
      item.dataset.key = info.key;
      item.setAttribute("role", "listitem");
      var button = doc.createElement("button");
      button.type = "button";
      button.className = "srv";
      button.setAttribute("aria-label", name + (info.own ? ", your party" : ""));
      button.style.background = "hsl(" + hueOf(name) + " 42% 36%)";
      button.style.color = "#ffffff";
      var text = doc.createElement("span");
      text.className = "initials";
      text.textContent = initials(name);
      var badge = doc.createElement("span");
      badge.className = "srv-badge";
      badge.hidden = true;
      var voiceMark = doc.createElement("span");
      voiceMark.className = "srv-mark voice";
      voiceMark.hidden = true;
      voiceMark.append(icon("speaker"));
      button.append(text, badge, voiceMark);
      if (info.own) {
        var own = doc.createElement("span");
        own.className = "srv-mark own";
        own.title = "Your party";
        own.append(icon("hat"));
        button.append(own);
      }
      item.append(button);
      if (info.own) el.list.prepend(item); else el.list.append(item);

      var frame = doc.createElement("iframe");
      frame.className = "view";
      frame.title = name;
      frame.setAttribute("allow", FRAME_ALLOW); // before the address, so the room may use the microphone
      frame.src = frameUrl(info.url, win.location.origin, theme);
      el.views.append(frame);

      var server = { key: info.key, name: name, url: info.url, origin: originOf(info.url), own: Boolean(info.own),
                     frame: frame, item: item, button: button, badge: badge, voiceMark: voiceMark, note: null,
                     state: cleanState(null), ready: false, timer: null, calls: 0 };
      servers.set(info.key, server);
      button.addEventListener("click", function () { show(server.key); });
      button.addEventListener("contextmenu", function (event) {
        event.preventDefault();
        openMenu(server, event.clientX, event.clientY);
      });
      tip(button, function () { return server.name + (server.own ? " (your party)" : ""); });
      frame.addEventListener("load", function () {
        clearTimeout(server.timer);
        if (!server.ready) server.timer = setTimeout(function () { notReady(server); }, READY_MS);
      });
      if (info.select !== false) show(server.key);
    }

    function removeServer(key) {
      var server = servers.get(key);
      if (!server) return;
      clearTimeout(server.timer);
      servers.delete(key);
      server.frame.remove();
      server.item.remove();
      if (server.note) server.note.remove();
      if (current === key) show("home");
      setFocus(false);
      updateDock();
    }

    function show(key) {
      if (key !== "home" && !servers.has(key)) key = "home";
      current = key;
      var home = key === "home";
      el.homeFrame.classList.toggle("current", home);
      el.home.classList.toggle("current", home);
      el.homeItem.classList.toggle("current", home);
      el.home.setAttribute("aria-current", home ? "page" : "false");
      servers.forEach(function (s) {
        var on = s.key === key;
        s.frame.classList.toggle("current", on && !s.note);
        if (s.note) s.note.classList.toggle("current", on);
        s.item.classList.toggle("current", on);
        s.button.classList.toggle("current", on);
        s.button.setAttribute("aria-current", on ? "page" : "false");
        post(s, { ppn: "visible", on: on });
      });
      doc.title = home ? "PrivatePartyNet" : servers.get(key).name + " - PrivatePartyNet";
      updateDock();
    }

    function post(server, msg) {
      if (server.frame.contentWindow && server.origin) server.frame.contentWindow.postMessage(msg, server.origin);
    }

    function render(server) {
      var s = server.state;
      server.badge.hidden = !s.requests;
      server.badge.textContent = s.requests ? badgeText(s.requests) : "";
      server.badge.title = s.requests ? s.requests + (s.requests === 1 ? " person waiting" : " people waiting") + " to join" : "";
      server.item.classList.toggle("unread", s.unread > 0);
      server.voiceMark.hidden = !s.voice;
      var label = server.name + (server.own ? ", your party" : "");
      if (s.unread) label += ", " + s.unread + " unread";
      if (s.requests) label += ", " + s.requests + " waiting to join";
      if (s.voice) label += ", you are in its voice channel";
      server.button.setAttribute("aria-label", label);
      updateDock();
    }

    // In a call on a server that is not shown: its controls at the foot of the rail.
    function updateDock() {
      var inCall = null;
      servers.forEach(function (s) { if (s.state.voice) inCall = s; });
      el.dock.hidden = !inCall || inCall.key === current;
      if (!inCall) return;
      var v = inCall.state.voice;
      el.dockServer.title = "In " + (v.channel || "a voice channel") + " on " + inCall.name + ": show it";
      el.dockServer.setAttribute("aria-label", el.dockServer.title);
      el.dockMute.setAttribute("aria-pressed", String(v.muted));
      el.dockMute.setAttribute("aria-label", v.muted ? "Unmute" : "Mute");
      el.dockMute.title = el.dockMute.getAttribute("aria-label");
      el.dockMute.replaceChildren(icon(v.muted ? "mic-off" : "mic"));
      el.dockDeafen.setAttribute("aria-pressed", String(v.deafened));
      el.dockDeafen.setAttribute("aria-label", v.deafened ? "Undeafen" : "Deafen");
      el.dockDeafen.title = el.dockDeafen.getAttribute("aria-label");
      el.dockDeafen.replaceChildren(icon(v.deafened ? "headphones-off" : "headphones"));
    }

    function inCallServer() {
      var found = null;
      servers.forEach(function (s) { if (s.state.voice) found = s; });
      return found;
    }

    el.dockServer.addEventListener("click", function () { var s = inCallServer(); if (s) show(s.key); });
    el.dockMute.addEventListener("click", function () { var s = inCallServer(); if (s) post(s, { ppn: "voice", action: "mute" }); });
    el.dockDeafen.addEventListener("click", function () { var s = inCallServer(); if (s) post(s, { ppn: "voice", action: "deafen" }); });
    el.dockLeave.addEventListener("click", function () { var s = inCallServer(); if (s) post(s, { ppn: "voice", action: "leave" }); });
    el.dockLeave.title = "Leave the voice channel";

    function setFocus(on) {
      el.shell.classList.toggle("focus", Boolean(on));
    }

    // A room that never says it is ready (an older host, or the page failed): say so where it would be.
    function notReady(server) {
      if (server.ready || !servers.has(server.key)) return;
      note(server, "This party did not open here.",
           "The host's PC did not answer, or it runs an older PrivatePartyNet. Try again, or close it and ask to join again.",
           [["Try again", function () { clearNote(server); server.frame.src = frameUrl(reloadUrl(server.url), win.location.origin, theme); }],
            ["Close", function () { closeServer(server); }]]);
    }

    function note(server, title, text, actions) {
      clearNote(server);
      var box = doc.createElement("div");
      box.className = "view-note";
      var card = doc.createElement("div");
      card.className = "card";
      var h = doc.createElement("h2");
      h.textContent = title;
      var p = doc.createElement("p");
      p.className = "muted";
      p.textContent = text;
      var row = doc.createElement("div");
      row.className = "row";
      actions.forEach(function (a, i) {
        var b = doc.createElement("button");
        b.type = "button";
        b.className = i === 0 ? "primary" : "secondary";
        b.textContent = a[0];
        b.addEventListener("click", a[1]);
        row.append(b);
      });
      card.append(h, p, row);
      box.append(card);
      el.views.append(box);
      server.note = box;
      show(current);
    }

    function clearNote(server) {
      if (server.note) server.note.remove();
      server.note = null;
    }

    // Leaving from the rail: the room says goodbye to the party itself; if it cannot, the app closes it.
    function closeServer(server) {
      if (server.ready) {
        post(server, { ppn: "leave" });
        setTimeout(function () { if (servers.has(server.key)) forceClose(server); }, 4000);
      } else {
        forceClose(server);
      }
    }

    function forceClose(server) {
      var bridge = api();
      if (bridge && bridge.room_call) bridge.room_call(server.key, "close_window", []).catch(function () {});
      removeServer(server.key);
    }

    // --- messages from the rooms --------------------------------------------------------------------------------

    win.addEventListener("message", function (event) {
      var server = null;
      servers.forEach(function (s) { if (s.frame.contentWindow === event.source) server = s; });
      if (!server || event.origin !== server.origin) return;
      var msg = event.data;
      if (!msg || typeof msg !== "object" || typeof msg.ppn !== "string") return;
      if (msg.ppn === "call") {
        roomCall(server, msg);
      } else if (msg.ppn === "state") {
        server.state = cleanState(msg);
        render(server);
      } else if (msg.ppn === "ready") {
        server.ready = true;
        clearTimeout(server.timer);
        clearNote(server);
        show(current);
        if (look) post(server, { ppn: "appearance", look: look });
        post(server, { ppn: "theme", theme: theme });
      }
    });

    function roomCall(server, msg) {
      var id = msg.id;
      server.calls += 1;
      var reply = function (ok, value) { post(server, { ppn: "result", id: id, ok: ok, value: ok ? value : undefined, error: ok ? undefined : value }); };
      if (ROOM_CALLS.indexOf(msg.method) < 0 || !Array.isArray(msg.args) || msg.args.length > 6) {
        reply(false, "That is not something a party window may ask for.");
        return;
      }
      if (msg.method === "set_fullscreen") setFocus(Boolean(msg.args[0]) && current === server.key);
      var bridge = api();
      if (!bridge || !bridge.room_call) {
        reply(false, "The app is not ready.");
        return;
      }
      bridge.room_call(server.key, msg.method, msg.args).then(function (value) { reply(true, value); },
        function (err) { reply(false, String((err && err.message) || err || "refused")); });
    }

    // --- what the app says --------------------------------------------------------------------------------------

    function handle(event) {
      if (!event || typeof event.type !== "string") return;
      if (event.type === "open") addServer(event);
      else if (event.type === "close") removeServer(event.key);
      else if (event.type === "info") showInfo(event.page, event.title);
      else if (event.type === "home") goHome(event.page);
      else if (event.type === "refused") refused(event.origin);
    }

    function refused(origin) {
      servers.forEach(function (s) {
        if (s.origin === origin) {
          s.ready = false;
          clearTimeout(s.timer);
          note(s, "This party was stopped.",
               "The PC that answered is not the one PrivatePartyNet checked when you asked to join (its certificate is different). " +
               "Close it and ask to join again. If it happens again, someone on the network may be standing in for the host.",
               [["Close", function () { forceClose(s); }]]);
        }
      });
    }

    function setTheme(next) {
      theme = next;
      doc.documentElement.dataset.theme = next;
      servers.forEach(function (s) { post(s, { ppn: "theme", theme: next }); });
    }

    function setLook(next) {
      look = next;
      if (win.PPNAppearance) win.PPNAppearance.apply(doc, next);
      servers.forEach(function (s) { post(s, { ppn: "appearance", look: next }); });
    }

    function poll() {
      var bridge = api();
      if (!bridge || !bridge.shell_state) return;
      bridge.shell_state().then(function (s) {
        if (!s) return;
        (s.events || []).forEach(handle);
        var unread = s.dm_unread || 0;
        el.homeBadge.hidden = !unread;
        el.homeBadge.textContent = unread ? badgeText(unread) : "";
        el.home.setAttribute("aria-label", unread ? "Home, " + unread + " unread direct messages" : "Home");
        if (s.theme && s.theme !== theme) setTheme(s.theme);
        if (s.appearance && JSON.stringify(s.appearance) !== JSON.stringify(look)) setLook(s.appearance);
      }).catch(function () { /* the next poll tries again */ });
    }

    // --- Home, Settings, About and How --------------------------------------------------------------------------

    function homeNav() {
      try {
        return el.homeFrame.contentWindow.PPNNav || null;
      } catch (err) {
        return null;
      }
    }

    function goHome(page) {
      show("home");
      var nav = homeNav();
      if (nav && page) nav.go(page);
    }

    function showInfo(page, title) {
      if (page !== "about" && page !== "how") return;
      el.overlayTitle.textContent = title || "";
      el.overlayFrame.src = page + ".html?embedded=1";
      el.overlay.hidden = false;
      el.overlayClose.focus();
    }

    function hideInfo() {
      el.overlay.hidden = true;
      el.overlayFrame.src = "about:blank";
    }

    el.overlayClose.addEventListener("click", hideInfo);
    el.overlay.addEventListener("click", function (event) { if (event.target === el.overlay) hideInfo(); });
    doc.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !el.overlay.hidden) hideInfo();
      if (event.key === "Escape" && !el.menu.hidden) closeMenu();
    });

    el.home.addEventListener("click", function () { show("home"); });
    el.settings.addEventListener("click", function () { goHome("settings"); });
    el.add.addEventListener("click", function (event) {
      var r = el.add.getBoundingClientRect();
      menu([["Start or show your party", function () { goHome("home"); }],
            ["Join with an invite", function () { goHome("join"); }],
            ["Nearby PCs", function () { goHome("nearby"); }]], r.right + 8, r.top);
      event.stopPropagation();
    });
    tip(el.home, function () { return "Home"; });
    tip(el.add, function () { return "Start a party or join one"; });
    tip(el.settings, function () { return "Settings"; });
    tip(el.dockServer, function () { return el.dockServer.title; });

    // --- the right-click menu and the names beside the rail -----------------------------------------------------

    function openMenu(server, x, y) {
      var items = [["Show", function () { show(server.key); }]];
      if (!server.own) items.push(["Leave this party", function () { closeServer(server); }, true]);
      else items.push(["Party settings on Home", function () { goHome("home"); }]);
      menu(items, x, y);
    }

    function menu(items, x, y) {
      el.menu.textContent = "";
      items.forEach(function (it) {
        var b = doc.createElement("button");
        b.type = "button";
        b.setAttribute("role", "menuitem");
        b.textContent = it[0];
        if (it[2]) b.className = "danger";
        b.addEventListener("click", function () { closeMenu(); it[1](); });
        el.menu.append(b);
      });
      el.menu.hidden = false;
      var w = el.menu.offsetWidth, h = el.menu.offsetHeight;
      el.menu.style.left = Math.min(x, win.innerWidth - w - 8) + "px";
      el.menu.style.top = Math.min(y, win.innerHeight - h - 8) + "px";
      var first = el.menu.querySelector("button");
      if (first) first.focus();
    }

    function closeMenu() {
      el.menu.hidden = true;
    }

    doc.addEventListener("click", function (event) { if (!el.menu.contains(event.target)) closeMenu(); });

    function tip(node, text) {
      function on() {
        var r = node.getBoundingClientRect();
        el.tip.textContent = text();
        el.tip.hidden = false;
        el.tip.style.left = (r.right + 12) + "px";
        el.tip.style.top = (r.top + r.height / 2 - el.tip.offsetHeight / 2) + "px";
      }
      function off() { el.tip.hidden = true; }
      node.addEventListener("mouseenter", on);
      node.addEventListener("focus", on);
      node.addEventListener("mouseleave", off);
      node.addEventListener("blur", off);
    }

    // This page takes no files: a file dropped on the rail must not replace the window with the file.
    ["dragover", "drop"].forEach(function (type) {
      doc.addEventListener(type, function (event) {
        if (event.dataTransfer && Array.prototype.indexOf.call(event.dataTransfer.types, "Files") >= 0) {
          event.preventDefault();
          event.dataTransfer.dropEffect = "none";
        }
      });
    });

    // For this window's own pages (same origin): Settings changes show at once, and Home's buttons can open views.
    win.PPNShell = {
      look: setLook,
      theme: setTheme,
      show: show,
      home: goHome,
      info: showInfo,
      // what the app's own window test reads (app.py window_test): each room, whether it signed in, and its calls
      servers: function () {
        return Array.from(servers.values()).map(function (s) {
          return { key: s.key, origin: s.origin, ready: s.ready, calls: s.calls, stopped: Boolean(s.note) };
        });
      },
    };

    function begin() {
      if (polling) return;
      var bridge = api();
      if (bridge && bridge.appearance) bridge.appearance().then(function (a) { if (a) setLook(a); }).catch(function () {});
      poll();
      polling = setInterval(poll, POLL_MS);
    }

    if (api()) begin();
    else win.addEventListener("pywebviewready", begin);
  }

  return { ROOM_CALLS: ROOM_CALLS, FRAME_ALLOW: FRAME_ALLOW, initials: initials, hueOf: hueOf, originOf: originOf,
           frameUrl: frameUrl, reloadUrl: reloadUrl, badgeText: badgeText, cleanState: cleanState, start: start };
});
