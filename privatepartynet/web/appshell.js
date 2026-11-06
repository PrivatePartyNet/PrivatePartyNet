/*
 * The one window (shell.html): the rail of servers on the far left, and beside it one view at a time. Home is the
 * control panel (control.html, from this same origin, so it uses this window's bridge to the app through
 * framebridge.js). Each server is its room in a frame from the server's own address; a room asks the app for
 * things through postMessage (roombridge.js), and this page passes on only the calls a server's room may make
 * (ROOM_CALLS), only from that frame and that origin, keyed by the server, to Controller.room_call.
 *
 * Hidden frames stay loaded, so a call keeps going while another server is shown; the rail then shows the call's
 * controls at its foot.
 *
 * The rail lists every server on this PC's list, for good (K, November 5th: a server you start or join "should
 * permanently go on their list of servers"): your own once you have made one, then each server joined. The app says
 * which (shell_state.servers) and how each stands; a server whose room is not open shows a card in its place (start it,
 * connecting, or out of reach), and so does a room that never signs in (a host too old for the one window). Rooms come and go with the app's open and close
 * events; a server leaves the rail only when it is left for good.
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

  var ROOM_CALLS = ["close_window", "set_fullscreen", "keep_chat", "keep_blob", "appearance", "my_picture", "server_look"];
  var POLL_MS = 1000;
  var READY_MS = 25000;
  var FRAME_ALLOW = "microphone; fullscreen; clipboard-write; autoplay; display-capture";
  // The sound each event plays (public-domain sounds K picked on November 5th; About names where they come from), and
  // the group in Settings that turns it off. Yours and other people's joins share a sound, as do mute and deafen.
  var SOUND_FILES = {
    join: "sound-join.mp3", leave: "sound-leave.mp3", "peer-join": "sound-join.mp3", "peer-leave": "sound-leave.mp3",
    mute: "sound-mute.ogg", unmute: "sound-mute.ogg", deafen: "sound-mute.ogg", undeafen: "sound-mute.ogg",
    message: "sound-message.ogg", request: "sound-request.ogg",
  };
  var SOUND_GROUP = {
    join: "voice", leave: "voice", "peer-join": "voice", "peer-leave": "voice", mute: "mute", unmute: "mute", deafen: "mute",
    undeafen: "mute", message: "messages", request: "requests",
  };

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

    var servers = new Map(); // key -> { key, name, url, origin, own, frame, item, button, badge, voiceMark, state, ready, listed }
    var current = "home";
    var theme = doc.documentElement.dataset.theme || "dark";
    var look = null;
    var polling = null;
    var sounds = { volume: 60, off: [] };
    var lastPlayed = {};
    var played = [];
    var picture = null; // this person's picture's hash, from the app; rooms hand it to their servers

    // One of SOUND_FILES' events, unless its group is off or it just played (`force`: a Play button in Settings).
    function sound(name, force) {
      var file = SOUND_FILES[name];
      if (!file) return false;
      if (!force && sounds.off.indexOf(SOUND_GROUP[name]) >= 0) return false;
      var now = Date.now();
      if (!force && lastPlayed[name] && now - lastPlayed[name] < 350) return false;
      lastPlayed[name] = now;
      played.push(name);
      if (played.length > 50) played.shift();
      if (!sounds.volume) return false;
      try {
        var audio = new Audio(file);
        audio.volume = Math.max(0, Math.min(1, sounds.volume / 100));
        var playing = audio.play();
        if (playing && playing.catch) playing.catch(function () { /* before the first click the browser may say no */ });
      } catch (err) {
        return false;
      }
      return true;
    }

    function setSounds(next) {
      if (!next) return;
      sounds = { volume: typeof next.volume === "number" ? next.volume : 60, off: Array.isArray(next.off) ? next.off.slice() : [] };
    }

    function api() {
      return win.pywebview && win.pywebview.api;
    }

    function icon(name) {
      return win.PPNIcons ? win.PPNIcons.node(name) : doc.createElement("span");
    }

    // --- the rail -----------------------------------------------------------------------------------------------

    // A server's place on the rail: its button (icon or initials, badges) whether or not its room is open.
    function ensureItem(info) {
      var server = servers.get(info.key);
      if (server) return server;
      var name = String(info.name || "Server").slice(0, 60);
      var item = doc.createElement("div");
      item.className = "srv-item";
      item.dataset.key = info.key;
      item.setAttribute("role", "listitem");
      var button = doc.createElement("button");
      button.type = "button";
      button.className = "srv";
      var text = doc.createElement("span");
      text.className = "initials";
      var badge = doc.createElement("span");
      badge.className = "srv-badge";
      badge.hidden = true;
      var voiceMark = doc.createElement("span");
      voiceMark.className = "srv-mark voice";
      voiceMark.hidden = true;
      voiceMark.append(icon("speaker"));
      var offMark = doc.createElement("span");
      offMark.className = "srv-mark off";
      offMark.hidden = true;
      button.append(text, badge, voiceMark, offMark);
      if (info.own) {
        var own = doc.createElement("span");
        own.className = "srv-mark own";
        own.title = "Your server";
        own.append(icon("hat"));
        button.append(own);
      }
      item.append(button);
      server = { key: info.key, name: name, url: "", origin: "", own: Boolean(info.own), frame: null, item: item,
                 button: button, initials: text, badge: badge, voiceMark: voiceMark, offMark: offMark, note: null,
                 state: cleanState(null), ready: false, timer: null, calls: 0, listed: false, rail: "", message: "",
                 icon: "", iconWanted: "" };
      servers.set(info.key, server);
      placeItem(server);
      button.addEventListener("click", function () { show(server.key); });
      button.addEventListener("contextmenu", function (event) {
        event.preventDefault();
        openMenu(server, event.clientX, event.clientY);
      });
      tip(button, function () {
        return server.name + (server.own ? " (your server)" : "") + (server.frame ? "" : railWords(server) ? ": " + railWords(server) : "");
      });
      setName(server, name);
      return server;
    }

    // Your own server first, then the others in the order the app lists them.
    function placeItem(server) {
      if (server.own) el.list.prepend(server.item);
      else el.list.append(server.item);
    }

    function setName(server, name) {
      server.name = String(name || server.name || "Server").slice(0, 60);
      server.initials.textContent = initials(server.name);
      if (!server.icon) server.button.style.background = "hsl(" + hueOf(server.name) + " 42% 36%)";
      server.button.style.color = "#ffffff";
      if (server.frame) server.frame.title = server.name;
      render(server);
    }

    // The icon the app keeps for a server (its room told it): fetched again only when it changes.
    function setIcon(server, hash) {
      hash = typeof hash === "string" && /^[0-9a-f]{64}$/.test(hash) ? hash : "";
      if (hash === server.iconWanted) return;
      server.iconWanted = hash;
      var old = server.button.querySelector("img");
      if (!hash) {
        if (old) old.remove();
        server.icon = "";
        server.initials.hidden = false;
        setName(server, server.name);
        return;
      }
      var bridge = api();
      if (!bridge || !bridge.server_icon) return;
      bridge.server_icon(server.key).then(function (answer) {
        if (server.iconWanted !== hash || !answer || typeof answer.src !== "string" || answer.src.indexOf("data:image/") !== 0) return;
        var img = server.button.querySelector("img") || doc.createElement("img");
        img.alt = "";
        img.src = answer.src;
        server.button.prepend(img);
        server.icon = hash;
        server.initials.hidden = true;
        server.button.style.background = "";
      }).catch(function () { server.iconWanted = ""; });
    }

    // The app's list: every server on it gets its place (and keeps it), with its name, icon and how it stands.
    function setList(list) {
      if (!Array.isArray(list)) return;
      var keys = new Set();
      list.forEach(function (info) {
        if (!info || typeof info.key !== "string") return;
        keys.add(info.key);
        var server = ensureItem(info);
        server.listed = true;
        if (info.name && info.name !== server.name) setName(server, info.name);
        setIcon(server, info.icon);
        var rail = String(info.state || "");
        var message = String(info.message || "").slice(0, 400);
        if (rail !== server.rail || message !== server.message) {
          server.rail = rail;
          server.message = message;
          render(server);
          if (!server.frame) {
            clearNote(server); // a card says how the server stands now, never how it stood
            if (current === server.key) showCard(server);
          }
        }
      });
      servers.forEach(function (server) {
        if (server.listed && !keys.has(server.key)) removeServer(server.key); // left for good, or forgotten
      });
      // in the app's order (your own first), moved only when it differs, so a hover or a focus is not disturbed
      var order = list.map(function (info) { return servers.get(info && info.key); }).filter(Boolean)
        .sort(function (a, b) { return (b.own ? 1 : 0) - (a.own ? 1 : 0); }).map(function (s) { return s.item; });
      servers.forEach(function (s) { if (order.indexOf(s.item) < 0) order.push(s.item); });
      var now = Array.prototype.slice.call(el.list.children);
      if (order.some(function (item, i) { return now[i] !== item; })) order.forEach(function (item) { el.list.append(item); });
    }

    // A room opens in its server's place (the app's "open" event).
    function addServer(info) {
      if (!info || typeof info.key !== "string" || typeof info.url !== "string") return;
      var server = ensureItem(info);
      if (info.name) setName(server, info.name);
      if (server.frame) {
        if (info.select !== false) show(info.key);
        return;
      }
      clearNote(server);
      var frame = doc.createElement("iframe");
      frame.className = "view";
      frame.title = server.name;
      frame.setAttribute("allow", FRAME_ALLOW); // before the address, so the room may use the microphone
      frame.src = frameUrl(info.url, win.location.origin, theme);
      el.views.append(frame);
      server.url = info.url;
      server.origin = originOf(info.url);
      server.frame = frame;
      server.ready = false;
      server.state = cleanState(null);
      frame.addEventListener("load", function () {
        clearTimeout(server.timer);
        if (!server.ready) server.timer = setTimeout(function () { notReady(server); }, READY_MS);
      });
      render(server);
      if (info.select !== false) show(server.key);
      else show(current);
    }

    // The room closed (left, or its server stopped). A server still on the list keeps its place, with its card.
    function closeRoom(key) {
      var server = servers.get(key);
      if (!server) return;
      clearTimeout(server.timer);
      if (server.frame) server.frame.remove();
      server.frame = null;
      server.ready = false;
      server.state = cleanState(null);
      setFocus(false);
      if (!server.listed) {
        removeServer(key);
        return;
      }
      render(server);
      show(current);
    }

    function removeServer(key) {
      var server = servers.get(key);
      if (!server) return;
      clearTimeout(server.timer);
      servers.delete(key);
      if (server.frame) server.frame.remove();
      server.item.remove();
      if (server.note) server.note.remove();
      if (current === key) show("home");
      setFocus(false);
      updateDock();
    }

    // What the rail says about a server whose room is not open.
    function railWords(server) {
      if (server.frame) return "";
      return { stopped: "off", starting: "starting", connecting: "connecting", offline: "out of reach" }[server.rail] || "";
    }

    // The card in the place of a server whose room is not open, with what can be done about it.
    function showCard(server) {
      var api_ = api();
      var leave = ["Leave server", function () { confirmLeave(server); }];
      var retry = ["Try again", function () { if (api_ && api_.connect_server) api_.connect_server(server.key).catch(function () {}); }];
      if (server.own) {
        if (server.rail === "starting") {
          note(server, "Starting your server...", "It opens here in a moment.", []);
        } else {
          note(server, server.name + " is off.",
               "People cannot reach your server until you start it. It starts with PrivatePartyNet unless you turned that off on Home.",
               [["Start it", function () { if (api_ && api_.start_own) api_.start_own().catch(function () {}); }],
                ["Server settings on Home", function () { goHome("home"); }]]);
        }
      } else if (server.rail === "connecting") {
        note(server, "Connecting to " + server.name + "...", server.message || "Reaching the host.", [leave]);
      } else {
        note(server, "Can't reach " + server.name + " right now.",
             (server.message ? server.message + " " : "") + "The host may be off. Your saved messages from it are on Home, under Saved chats.",
             [retry, ["Read saved messages", function () { goHome("chats"); }], leave]);
      }
    }

    function confirmLeave(server) {
      var r = server.button.getBoundingClientRect();
      menu([["Leave " + server.name + " for good", function () { leaveForGood(server); }, true],
            ["Cancel", function () {}]], r.right + 8, r.top);
    }

    // Leaving a server: its room says goodbye to the server, and the app takes it off the list.
    function leaveForGood(server) {
      var bridge = api();
      if (server.frame && server.ready) post(server, { ppn: "leave" });
      if (bridge && bridge.forget_joined) bridge.forget_joined(server.key).catch(function () {});
      server.listed = false;
      setTimeout(function () { removeServer(server.key); }, server.frame && server.ready ? 1500 : 0);
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
        if (on && !s.frame && !s.note) showCard(s);
        if (s.frame) s.frame.classList.toggle("current", on && !s.note);
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
      if (server.frame && server.frame.contentWindow && server.origin) server.frame.contentWindow.postMessage(msg, server.origin);
    }

    function render(server) {
      var s = server.state;
      server.badge.hidden = !s.requests;
      server.badge.textContent = s.requests ? badgeText(s.requests) : "";
      server.badge.title = s.requests ? s.requests + (s.requests === 1 ? " person waiting" : " people waiting") + " to join" : "";
      server.item.classList.toggle("has-unread", s.unread > 0); // not "unread": that is the yellow count badge (ui.css)
      server.voiceMark.hidden = !s.voice;
      var off = !server.frame && server.rail && server.rail !== "open";
      server.item.classList.toggle("away", Boolean(off));
      server.offMark.hidden = !off || server.rail === "connecting" || server.rail === "starting";
      server.item.classList.toggle("connecting", !server.frame && (server.rail === "connecting" || server.rail === "starting"));
      var label = server.name + (server.own ? ", your server" : "");
      if (off && railWords(server)) label += ", " + railWords(server);
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
      note(server, "This server did not open here.",
           "The host's PC did not answer, or it runs an older PrivatePartyNet. Try again, or close it and choose it on the left again.",
           [["Try again", function () { clearNote(server); if (server.frame) server.frame.src = frameUrl(reloadUrl(server.url), win.location.origin, theme); }],
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
      card.append(h, p);
      if (actions.length) card.append(row);
      box.append(card);
      el.views.append(box);
      server.note = box;
      box.classList.toggle("current", current === server.key);
      if (server.frame) server.frame.classList.toggle("current", false);
    }

    function clearNote(server) {
      if (server.note) server.note.remove();
      server.note = null;
      if (server.frame) server.frame.classList.toggle("current", current === server.key);
    }

    // Closing a room that never opened: the room says goodbye to its server; if it cannot, the app closes it.
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
      closeRoom(server.key);
    }

    // --- messages from the rooms --------------------------------------------------------------------------------

    win.addEventListener("message", function (event) {
      var server = null;
      servers.forEach(function (s) { if (s.frame && s.frame.contentWindow === event.source) server = s; });
      if (!server || event.origin !== server.origin) return;
      var msg = event.data;
      if (!msg || typeof msg !== "object" || typeof msg.ppn !== "string") return;
      if (msg.ppn === "call") {
        roomCall(server, msg);
      } else if (msg.ppn === "sound" && typeof msg.name === "string") {
        sound(msg.name);
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
      } else if (msg.ppn === "leave-server") {
        if (server.own) return;
        show(server.key);
        confirmLeave(server);
      }
    });

    function roomCall(server, msg) {
      var id = msg.id;
      server.calls += 1;
      var reply = function (ok, value) { post(server, { ppn: "result", id: id, ok: ok, value: ok ? value : undefined, error: ok ? undefined : value }); };
      if (ROOM_CALLS.indexOf(msg.method) < 0 || !Array.isArray(msg.args) || msg.args.length > 6) {
        reply(false, "That is not something a server's room may ask for.");
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
      else if (event.type === "close") closeRoom(event.key);
      else if (event.type === "show") show(event.key);
      else if (event.type === "info") showInfo(event.page, event.title);
      else if (event.type === "home") goHome(event.page);
      else if (event.type === "refused") refused(event.origin);
    }

    function refused(origin) {
      servers.forEach(function (s) {
        if (s.origin === origin) {
          s.ready = false;
          clearTimeout(s.timer);
          note(s, "This server was stopped here.",
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
        setList(s.servers);
        if (typeof s.picture === "string" && s.picture !== picture) {
          var first = picture === null;
          picture = s.picture;
          if (!first) servers.forEach(function (server) { if (server.ready) post(server, { ppn: "picture" }); });
        }
        if (s.theme && s.theme !== theme) setTheme(s.theme);
        if (s.appearance && JSON.stringify(s.appearance) !== JSON.stringify(look)) setLook(s.appearance);
        setSounds(s.sounds);
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
      var hasOwn = false;
      servers.forEach(function (s) { if (s.own) hasOwn = true; });
      menu([[hasOwn ? "Your server" : "Create your server", function () { goHome("home"); }],
            ["Join a server with an invite", function () { goHome("join"); }],
            ["Nearby PCs", function () { goHome("nearby"); }]], r.right + 8, r.top);
      event.stopPropagation();
    });
    tip(el.home, function () { return "Home"; });
    tip(el.add, function () { return "Create your server, or join one"; });
    tip(el.settings, function () { return "Settings"; });
    tip(el.dockServer, function () { return el.dockServer.title; });

    // --- the right-click menu and the names beside the rail -----------------------------------------------------

    function openMenu(server, x, y) {
      var bridge = api();
      var items = [["Show", function () { show(server.key); }]];
      if (server.own) {
        if (server.frame) {
          items.push(["Copy an invite", function () {
            if (!bridge || !bridge.invite) return;
            bridge.invite().then(function (answer) {
              if (answer && answer.ok && win.navigator.clipboard) win.navigator.clipboard.writeText(answer.invite).catch(function () {});
            }).catch(function () {});
          }]);
        }
        items.push(["Your server on Home", function () { goHome("home"); }]);
        if (server.frame) items.push(["Stop your server", function () { if (bridge && bridge.stop) bridge.stop().catch(function () {}); }, true]);
        else items.push(["Start your server", function () { if (bridge && bridge.start_own) bridge.start_own().catch(function () {}); }]);
      } else {
        if (!server.frame) items.push(["Try again", function () { if (bridge && bridge.connect_server) bridge.connect_server(server.key).catch(function () {}); }]);
        items.push(["Leave server", function () { confirmLeave(server); }, true]);
      }
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
      sound: sound,
      setSounds: setSounds,
      played: function () { return played.slice(); },
      // what the app's own window test reads (app.py window_test): each room, whether it signed in, and its calls
      servers: function () {
        return Array.from(servers.values()).map(function (s) {
          return { key: s.key, origin: s.origin, ready: s.ready, calls: s.calls, stopped: Boolean(s.note && s.frame),
                   listed: s.listed, rail: s.rail, open: Boolean(s.frame), name: s.name };
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

  return { ROOM_CALLS: ROOM_CALLS, FRAME_ALLOW: FRAME_ALLOW, SOUND_FILES: SOUND_FILES, SOUND_GROUP: SOUND_GROUP, initials: initials, hueOf: hueOf, originOf: originOf,
           frameUrl: frameUrl, reloadUrl: reloadUrl, badgeText: badgeText, cleanState: cleanState, start: start };
});
