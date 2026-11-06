/*
 * A server's room, for the host and for every member. Each pair of online members keeps one WebRTC connection.
 * Screen shares and voice travel over those connections. Chat and the roster come through the server's event stream.
 *
 * The middle of the room shows one channel at a time, as Discord does: a text channel's chat, or a voice channel's
 * screens (K, November 5th: chats are tied to a text channel, and screens to a voice channel). A screen can only be
 * shared from inside a voice channel, goes only to the people in that channel, and shows only to them.
 *
 * Connection rule: between two members, the one with the lower id makes the offer. The other side answers.
 * That way each pair negotiates exactly once, and a reconnect simply renegotiates.
 *
 * Several screens: each person shares up to PartyRTC.SLOTS screens. Share n of a person travels in slot n of every
 * connection they have, and the server's roster says which slots are live and what each one is called.
 *
 * The stage has two views. The grid shows every screen as a tile you can move and resize (layout.js does the maths and
 * the snapping). Clicking a tile switches to the focus view: that screen large, the others as previews along the
 * bottom. Right-click a tile to hide it; the sender then stops sending it to you.
 */
(function () {
  "use strict";

  var RTC = window.PartyRTC;
  var Layout = window.PrivatePartyNetLayout;
  var Stats = window.PrivatePartyNetNetStats;
  var CaptureSize = window.PrivatePartyNetCaptureSize;

  var PREFS = {
    receive: "privatepartynet.receive",
    upscale: "privatepartynet.upscale",
    ceiling: "privatepartynet.ceiling",
    fps: "privatepartynet.fps",
    name: "privatepartynet.name",
    pointer: "privatepartynet.pointer",
    side: "privatepartynet.side",
  };

  var EDGES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
  var DRAG_THRESHOLD = 4; // pixels the pointer must move before a press becomes a drag
  var MAX_INFLIGHT = 3; // frames being decoded at once; a fourth is dropped rather than queued

  var ICONS = {
    up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
    down: '<path d="M12 5v14M6 13l6 6 6-6"/>',
    fps: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M8 5v14M16 5v14M3 10h5M3 14h5M16 10h5M16 14h5"/>',
    size: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    cpu: '<rect x="7" y="7" width="10" height="10" rx="1"/><path d="M9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    expand: '<path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/>',
    shrink: '<path d="M4 9h5V4M20 9h-5V4M4 15h5v5M20 15h-5v5"/>',
  };

  var el = {
    partyName: document.getElementById("partyName"),
    count: document.getElementById("count"),
    goLive: document.getElementById("goLive"),
    liveSplit: document.getElementById("liveSplit"),
    liveSettings: document.getElementById("liveSettings"),
    livePanel: document.getElementById("livePanel"),
    viewBtn: document.getElementById("viewBtn"),
    viewPanel: document.getElementById("viewPanel"),
    monitor: document.getElementById("monitor"),
    monitorPick: document.getElementById("monitorPick"),
    ceiling: document.getElementById("ceiling"),
    fps: document.getElementById("fps"),
    pointer: document.getElementById("pointer"),
    sideBtn: document.getElementById("sideBtn"),
    side: document.getElementById("side"),
    receive: document.getElementById("receive"),
    upscale: document.getElementById("upscale"),
    upscaleNote: document.getElementById("upscaleNote"),
    resetLayout: document.getElementById("resetLayout"),
    showHidden: document.getElementById("showHidden"),
    pip: document.getElementById("pipBtn"),
    full: document.getElementById("fullBtn"),
    stage: document.getElementById("stage"),
    tiles: document.getElementById("tiles"),
    focusView: document.getElementById("focusView"),
    primary: document.getElementById("primary"),
    strip: document.getElementById("strip"),
    guides: document.getElementById("guides"),
    empty: document.getElementById("empty"),
    mine: document.getElementById("mine"),
    myShares: document.getElementById("myShares"),
    roster: document.getElementById("roster"),
    myName: document.getElementById("myName"),
    messages: document.getElementById("messages"),
    chatForm: document.getElementById("chatForm"),
    chatText: document.getElementById("chatText"),
    statsbar: document.getElementById("statsbar"),
    cpuChip: document.getElementById("cpuChip"),
    cpuValue: document.getElementById("cpuValue"),
    peerChips: document.getElementById("peerChips"),
    tip: document.getElementById("tip"),
    ctxMenu: document.getElementById("ctxMenu"),
    chatTitle: document.getElementById("chatTitle"),
    channelList: document.getElementById("channelList"),
    addText: document.getElementById("addText"),
    addVoice: document.getElementById("addVoice"),
    lightbox: document.getElementById("lightbox"),
    voiceBar: document.getElementById("voiceBar"),
    voiceName: document.getElementById("voiceName"),
    voiceState: document.getElementById("voiceState"),
    muteBtn: document.getElementById("muteBtn"),
    deafenBtn: document.getElementById("deafenBtn"),
    voiceSettings: document.getElementById("voiceSettings"),
    voiceLeave: document.getElementById("voiceLeave"),
    voicePanel: document.getElementById("voicePanel"),
    voiceInput: document.getElementById("voiceInput"),
    voiceOutput: document.getElementById("voiceOutput"),
    voicePtt: document.getElementById("voicePtt"),
    voiceVolumes: document.getElementById("voiceVolumes"),
    requestsBox: document.getElementById("requestsBox"),
    requestList: document.getElementById("requestList"),
    status: document.getElementById("status"),
    chat: document.getElementById("chat"),
    titleIcon: document.getElementById("titleIcon"),
    titleText: document.getElementById("titleText"),
    titleNote: document.getElementById("titleNote"),
    emptyTitle: document.getElementById("emptyTitle"),
    emptyText: document.getElementById("emptyText"),
    voiceRoom: document.getElementById("voiceRoom"),
    voiceJoin: document.getElementById("voiceJoin"),
    serverMenuBtn: document.getElementById("serverMenuBtn"),
    serverMark: document.getElementById("serverMark"),
    serverPanel: document.getElementById("serverPanel"),
    serverNameInput: document.getElementById("serverNameInput"),
    serverIconPreview: document.getElementById("serverIconPreview"),
    serverIconFile: document.getElementById("serverIconFile"),
    serverIconClear: document.getElementById("serverIconClear"),
    serverProblem: document.getElementById("serverProblem"),
    serverSave: document.getElementById("serverSave"),
    serverClose: document.getElementById("serverClose"),
  };

  var me = null;
  var hostId = null;
  var members = new Map(); // id -> member, as the server last described it
  var peers = new Map(); // id -> { pc, wants }; wants maps a slot to what this page asks of that member's slot
  var tiles = new Map(); // tile key -> tile element
  var tileInfo = new Map(); // tile key -> { member, slot, name, width, height, track }
  var streams = new Map(); // tile key -> { track, stream }, so a tile keeps its stream while its track is the same
  var rects = new Map(); // tile key -> { x, y, w, h } in the grid
  var hiddenKeys = new Set(); // tile keys the user chose not to show
  var shares = new Map(); // key -> this page's own share: { key, slot, label, width, height, track, stop }
  var layoutManual = false; // false: the grid arranges the tiles; true: the user has placed them
  var focusKey = null; // the tile shown large, or null for the grid
  var animateNext = false;
  var zTop = 10;
  var press = null; // a pointer press on a tile that may become a drag, a resize or a click
  var pickerCount = 0;
  var myCeiling = { height: null, fps: null };
  var resizeTimers = new Map();
  var streamPrefs = new Map(); // tile key -> { receive, upscale } chosen for that one stream, over the View panel's
  var streamPanel = null; // the open per-stream settings panel: { key, node }
  var statSamples = new Map(); // peer id -> the previous normalized stats report
  var summaries = new Map(); // peer id -> the latest connection summary
  var hoverId = null; // the peer whose chip the pointer is over
  var statsBusy = false;
  var lastCpu = null;
  var lastCpuDetail = null; // the split behind that figure: the app itself, its browser engine, the whole PC
  var upscaleRunning = false;
  var upscaleBusyMs = 0; // time spent drawing upscaled frames since the last CPU readout

  var PARAMS = new URLSearchParams(location.search);

  restorePrefs();

  var events = new EventSource("/events");
  events.onmessage = function (event) {
    handle(JSON.parse(event.data));
  };
  events.onerror = function () {
    if (events.readyState === EventSource.CLOSED) {
      notice("The connection to the server ended. Choose the server on the left to connect again.");
    }
  };

  // The theme comes from the server; a room opened with its own ?theme= keeps that choice.
  // The app opens a joined server's room with ?feed= (this PC's own screen feed) and ?token=; its ?theme= wins.
  var FEED = PARAMS.get("feed");
  var FEED_TOKEN = PARAMS.get("feed_token");
  var THEME_OVERRIDE = PARAMS.get("theme");
  if (THEME_OVERRIDE) document.documentElement.dataset.theme = THEME_OVERRIDE;
  function usesFrames() {
    return Boolean(FEED) || document.body.dataset.capture === "frames";
  }

  function feedPath(path, query) {
    return FEED ? FEED + path + "?" + query + "&token=" + encodeURIComponent(FEED_TOKEN) : path + "?" + query;
  }

  // The key of the screen picked in the Screen menu, or null when this window uses the browser's own picker.
  function selectedKey() {
    return usesFrames() && el.monitor.value ? "monitor-" + el.monitor.value : null;
  }

  function selectedLabel() {
    var option = el.monitor.selectedOptions[0];
    return option ? option.textContent.replace(/ \(.*$/, "") : "Screen";
  }

  // The screens this PC has, for the Screen menu. Only frame-capturing windows (the host, or a join window) use it.
  function loadMonitors() {
    var url = FEED ? FEED + "/monitors?token=" + encodeURIComponent(FEED_TOKEN) : "/monitors";
    fetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error("screen list returned " + response.status);
      return response.json();
    }).then(function (screens) {
      el.monitor.textContent = "";
      screens.forEach(function (screen) {
        var option = document.createElement("option");
        option.value = String(screen.index);
        option.dataset.width = String(screen.width);
        option.dataset.height = String(screen.height);
        option.textContent = "Screen " + screen.index + " (" + screen.width + "x" + screen.height + ")" +
          (screen.primary ? " main" : "");
        if (screen.primary) option.selected = true;
        el.monitor.append(option);
      });
      el.monitorPick.hidden = screens.length === 0;
      updateGoLive();
    }).catch(function (err) {
      report("screen list failed: " + err.message);
    });
  }

  // Picking another screen only changes what the button does next. Screens already live keep sharing.
  el.monitor.addEventListener("change", updateGoLive);

  if (usesFrames()) loadMonitors();

  function applyTheme(theme) {
    if (!THEME_OVERRIDE) document.documentElement.dataset.theme = theme;
  }

  function handle(msg) {
    if (msg.type === "theme") {
      applyTheme(msg.theme);
      return;
    }
    if (msg.type === "hello") {
      applyTheme(msg.theme);
      me = msg.you;
      hostId = msg.host;
      lastVoice = me.voice || null;
      setServerLook(msg.party, msg.icon);
      el.myName.value = me.name;
      syncPicture();
      RTC.setStun(!!msg.stun, msg.turn);
      chat.hello(msg);
      setStorage(msg.storage);
      if (window.PPNEmbedded) window.PPNEmbedded.send({ ppn: "ready" }); // the one window: this room is signed in
      var savedName = readPref(PREFS.name);
      if (savedName && savedName !== me.name) post("/name", { name: savedName }).catch(function () {});
    } else if (msg.type === "roster") {
      hostId = msg.host;
      members = new Map(msg.members.map(function (m) { return [m.id, m]; }));
      if (me && members.has(me.id)) me = members.get(me.id);
      voiceChanged();
      reconcile();
      renderRoster();
      chat.setMembers(members);
      voice.sync();
      checkCompanions();
      peers.forEach(function (entry, id) { attachAll(id); }); // who is in my voice channel may have changed
      sendQualityToEveryone();
      picturesChanged();
    } else if (msg.type === "history") {
      chat.history(msg);
    } else if (msg.type === "chat") {
      chat.message(msg);
    } else if (msg.type === "deleted") {
      chat.deleted(msg);
    } else if (msg.type === "storage") {
      setStorage(msg);
    } else if (msg.type === "ice") {
      RTC.setStun(!!msg.stun, msg.turn); // connections made from now on can cross routers; the ones already up are fine
    } else if (msg.type === "channels") {
      chat.setChannels(msg.channels);
    } else if (msg.type === "role") {
      if (me) me.role = msg.role;
      chat.setRole(msg.role);
      setStorage(chat.storage()); // an owner's or admin's File limits link
      renderRoster();
      notice(msg.role === "guest" ? "You are now a guest: you can watch and read, not talk or post."
        : "Your role is now " + (msg.role === "host" ? "owner" : msg.role) + ".");
    } else if (msg.type === "join_request") {
      if (!requests.has(msg.id)) shellSound("request");
      requests.set(msg.id, msg);
      renderRequests();
    } else if (msg.type === "join_resolved") {
      requests.delete(msg.id);
      renderRequests();
    } else if (msg.type === "signal") {
      signal(msg);
    } else if (msg.type === "kicked" || msg.type === "left") {
      events.close();
      teardownAll();
      notice(msg.type === "kicked" ? "The owner or an admin removed you from this server." : "You left the server.");
    } else if (msg.type === "stop") {
      events.close();
      teardownAll();
      notice("The host stopped the server. It stays on your list; it opens again when the host starts it.");
    } else if (msg.type === "server") {
      setServerLook(msg.party || msg.name, msg.icon);
    }
    updateCount();
    renderView();
  }

  // --- connections -------------------------------------------------------------------------------------------

  function reconcile() {
    var online = new Set();
    members.forEach(function (member) {
      if (member.online && me && member.id !== me.id) online.add(member.id);
    });
    peers.forEach(function (entry, id) {
      if (!online.has(id)) closePeer(id);
    });
    online.forEach(function (id) {
      if (!peers.has(id) && me.id < id) offerTo(id);
    });
    renderTiles();
  }

  function makePeer(id) {
    var entry = { pc: RTC.newPeer(), wants: {} };
    peers.set(id, entry);
    return entry;
  }

  function closePeer(id) {
    var entry = peers.get(id);
    if (!entry) return;
    entry.pc.close();
    peers.delete(id);
    statSamples.delete(id);
    summaries.delete(id);
    renderTiles();
  }

  async function offerTo(id) {
    var entry = makePeer(id);
    RTC.addSlots(entry.pc);
    RTC.addAudio(entry.pc);
    try {
      var offer = await entry.pc.createOffer();
      offer.sdp = RTC.tuneOpus(offer.sdp);
      await entry.pc.setLocalDescription(offer);
      await RTC.gatherComplete(entry.pc);
      await send(id, { kind: "offer", sdp: entry.pc.localDescription.sdp });
    } catch (err) {
      notice("Could not connect to a member: " + err.message);
    }
  }

  async function answerFrom(id, sdp) {
    closePeer(id);
    var entry = makePeer(id);
    try {
      await entry.pc.setRemoteDescription({ type: "offer", sdp: sdp });
      RTC.prepareAnswer(entry.pc);
      renderTiles();
      attachAll(id);
      var answer = await entry.pc.createAnswer();
      answer.sdp = RTC.tuneOpus(answer.sdp);
      await entry.pc.setLocalDescription(answer);
      await RTC.gatherComplete(entry.pc);
      await send(id, { kind: "answer", sdp: entry.pc.localDescription.sdp });
      sendAllQualityTo(id);
      voice.sync();
    } catch (err) {
      notice("Could not answer a member: " + err.message);
    }
  }

  function signal(msg) {
    if (msg.kind === "offer") {
      answerFrom(msg.from, msg.sdp);
    } else if (msg.kind === "answer") {
      var entry = peers.get(msg.from);
      if (!entry) return;
      entry.pc.setRemoteDescription({ type: "answer", sdp: msg.sdp })
        .then(function () {
          renderTiles();
          attachAll(msg.from);
          sendAllQualityTo(msg.from);
          voice.sync();
        })
        .catch(function () {});
    } else if (msg.kind === "quality") {
      var peer = peers.get(msg.from);
      if (!peer) return;
      var slot = Number(msg.slot);
      if (msg.mode === "off") peer.wants[slot] = { mode: "off" };
      else if (msg.mode === "fit") peer.wants[slot] = { mode: "fit", width: msg.width, height: msg.height };
      else peer.wants[slot] = { mode: "percent", percent: msg.percent };
      var share = shareInSlot(slot);
      if (share) attachShare(msg.from, share);
    }
  }

  // Send one of this page's shares to one member, limited by what that member asked for in this slot and by the
  // Go Live ceiling. The member receives it in the same slot. A member who hid the screen gets nothing.
  function attachShare(id, share) {
    var entry = peers.get(id);
    if (!entry) return;
    if (!sameVoice(id)) { // only the people in this person's voice channel get their screens
      RTC.setScreenTrack(entry.pc, share.slot, null).catch(function () {});
      return;
    }
    RTC.setScreenTrack(entry.pc, share.slot, share.track)
      .then(function () { return RTC.limitSending(entry.pc, share.slot, share.track, entry.wants[share.slot], ceilingOf(share), share.source); })
      .catch(function () {
        // the connection may still be settling; the next quality message or share change tries again
      });
  }

  // The limit on what a share sends: its own settings, or the Go Live panel's when it has none.
  function ceilingOf(share) {
    if (!share.opts) return myCeiling;
    return { height: share.opts.ceiling === "source" ? null : Number(share.opts.ceiling), fps: share.opts.fps };
  }

  function attachAll(id) {
    shares.forEach(function (share) {
      attachShare(id, share);
    });
  }

  // Tell one member what this page wants from one of their slots: nothing, if the stream is hidden here. "Fit my
  // window" uses that tile's size on screen.
  function sendQualityTo(id, slot) {
    if (!peers.has(id)) return;
    var key = tileKey(id, slot);
    var wanted;
    if (hiddenKeys.has(key) || !sameVoice(id) || !watching()) {
      wanted = { mode: "off" }; // hidden, not in my voice channel, or nobody here is looking at the screens
    } else if (receiveFor(key) === "fit") {
      var tile = tiles.get(key);
      wanted = tile ? RTC.fitTo(tile.querySelector("video")) : RTC.fitTo(el.stage);
    } else {
      wanted = RTC.percentQuality(receiveFor(key));
    }
    send(id, Object.assign({ kind: "quality", slot: slot }, wanted)).catch(function () {
      // the member is not connected yet; the answer or the next change sends it again
    });
  }

  function sharesOf(id) {
    var member = members.get(id);
    return member && member.shares ? member.shares : [];
  }

  function sendAllQualityTo(id) {
    sharesOf(id).forEach(function (share) {
      sendQualityTo(id, share.slot);
    });
  }

  function sendQualityToEveryone() {
    peers.forEach(function (entry, id) {
      sendAllQualityTo(id);
    });
  }

  // --- voice channels and their screens ----------------------------------------------------------------------

  var lastVoice = null; // the voice channel the server last had this person in

  // The voice channel this person is in, as the server last said, or null.
  function myVoice() {
    return me && me.voice ? me.voice : null;
  }

  // True when member `id` is in this person's voice channel: only then do screens go between them.
  function sameVoice(id) {
    var member = members.get(id);
    return Boolean(myVoice() && member && member.online && member.voice === myVoice());
  }

  // Someone is looking at this person's voice channel's screens: the room is shown and the middle is that channel.
  function watching() {
    return Boolean(myVoice()) && shownView.kind === "voice" && shownView.channel === myVoice() && shellShown && !document.hidden;
  }

  // The server moved this person out of their voice channel, or into another: the screens they shared there end with it.
  function voiceChanged() {
    var now = myVoice();
    if (now === lastVoice) return;
    lastVoice = now;
    Array.from(shares.keys()).forEach(stopShare);
    updateGoLive();
  }

  // --- going live --------------------------------------------------------------------------------------------

  // The screen picker must open straight from the click, so nothing above awaits before getDisplayMedia.
  el.goLive.addEventListener("click", async function () {
    report("go live clicked (capture: " + (document.body.dataset.capture || "display") + ")");
    var key = selectedKey();
    if (key && shares.has(key)) {
      stopShare(key);
      return;
    }
    if (!myVoice()) {
      notice("Join a voice channel first: your screen goes to the people in your voice channel.");
      return;
    }
    if (me && me.role === "guest") {
      notice("Guests watch and listen; they do not share screens.");
      return;
    }
    if (shares.size >= RTC.SLOTS) {
      notice("Up to " + RTC.SLOTS + " screens can be shared at once. Stop one first.");
      return;
    }
    var slot = freeSlot();
    if (!key) key = "picker-" + (pickerCount += 1);
    var label = key.indexOf("monitor-") === 0 ? selectedLabel() : "Screen " + (slot + 1);
    var opts = currentOptions();
    try {
      var capture = usesFrames()
        ? frameCapture(opts, el.monitor.value, sizeReporter(key))
        : await pickerCapture(opts);
      var size = trackSize(capture.track);
      var share = { key: key, slot: slot, label: label, track: capture.track, stop: capture.stop, width: size.width, height: size.height, opts: opts };
      var source = usesFrames() ? monitorSource(el.monitor.value) : null;
      if (source) {
        share.source = source;
        share.width = source.width;
        share.height = source.height;
        share.governor = new CaptureSize.Governor(ceilingHeightOf(opts));
      }
      share.track.addEventListener("ended", function () { stopShare(key); });
      shares.set(key, share);
      myCeiling = ceilingFromChoices();
      peers.forEach(function (entry, id) {
        attachShare(id, share);
      });
      postShare(share, true);
      updateGoLive();
      renderMyShares();
      renderTiles();
    } catch (err) {
      report("go live failed: " + err.message);
      notice("Could not share the screen: " + err.message);
    }
  });

  // The browser's own screen picker, used by a member's window. Each click opens a picker and adds one share.
  async function pickerCapture(opts) {
    var stream = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: { ideal: opts.fps }, cursor: opts.pointer === "off" ? "never" : "always" },
      audio: false,
    });
    return { track: stream.getVideoTracks()[0], stop: function () {} };
  }

  // The host's window and a join window capture a chosen screen through the frame feed, so no picker is needed.
  // feedHeight is the height to capture at, or null for the screen's own size; left out, the Go Live ceiling decides.
  function frameCapture(opts, monitor, onSize, feedHeight) {
    var fps = opts.fps;
    var height = feedHeight !== undefined ? feedHeight : ceilingHeightOf(opts);
    var query = "fps=" + fps + (height ? "&height=" + height : "") + "&monitor=" + encodeURIComponent(monitor) +
      (opts.pointer === "off" ? "&pointer=0" : "");
    var canvas = document.createElement("canvas");
    var ctx = canvas.getContext("2d", { alpha: false });
    var controller = new AbortController();
    var drawn = 0;
    var since = performance.now();
    readFrames(feedPath("/frames", query), controller.signal, function (bitmap) {
      if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
        canvas.width = bitmap.width;
        canvas.height = bitmap.height;
        onSize(bitmap.width, bitmap.height);
      }
      ctx.drawImage(bitmap, 0, 0);
      bitmap.close();
      drawn += 1;
      var now = performance.now();
      if (now - since >= 10000) {
        // a line in the app's log, so the frame rate this page really achieves can be read back
        report("frame feed: " + ((drawn * 1000) / (now - since)).toFixed(1) + " fps drawn at " + canvas.width + "x" + canvas.height);
        drawn = 0;
        since = now;
      }
    }).catch(function () {
      // the feed ended (the share was stopped, or the app closed); the share ends with it
      report("frame feed ended");
    });
    var track = canvas.captureStream(fps).getVideoTracks()[0];
    return { track: track, stop: function () { controller.abort(); } };
  }

  // A frame share learns its picture size from its first frame. The server is told, so receivers can size the tile.
  function sizeReporter(key) {
    return function (width, height) {
      var share = shares.get(key);
      if (!share) return;
      if (share.source) {
        // the capture may be smaller than the screen; the server is told the screen's real size
        width = share.source.width;
        height = share.source.height;
      }
      if (share.width === width && share.height === height) return;
      share.width = width;
      share.height = height;
      postShare(share, true);
      renderTiles();
    };
  }

  function trackSize(track) {
    var settings = track.getSettings();
    return { width: settings.width || 0, height: settings.height || 0 };
  }

  // Read the length-prefixed JPEG stream from the server and decode several frames at once. A frame arriving while
  // MAX_INFLIGHT are already decoding is dropped, and a frame that finishes after a newer one is thrown away, so a
  // slow decode costs frames, never delay. Bytes are gathered in one growing buffer, not copied on every chunk.
  async function readFrames(url, signal, onFrame) {
    var response = await fetch(url, { signal: signal, cache: "no-store" });
    if (!response.ok) throw new Error("frames returned " + response.status);
    var reader = response.body.getReader();
    var buffer = new Uint8Array(1 << 20);
    var start = 0;
    var end = 0;
    var inflight = 0;
    var issued = 0;
    var newest = -1;

    function make(room) {
      if (end + room <= buffer.length) return;
      var used = end - start;
      if (used + room > buffer.length) {
        var bigger = new Uint8Array(Math.max(buffer.length * 2, used + room));
        bigger.set(buffer.subarray(start, end));
        buffer = bigger;
      } else {
        buffer.copyWithin(0, start, end);
      }
      start = 0;
      end = used;
    }

    function decode(jpeg) {
      var mine = issued;
      issued += 1;
      inflight += 1;
      createImageBitmap(new Blob([jpeg], { type: "image/jpeg" })).then(function (bitmap) {
        inflight -= 1;
        if (mine > newest && !signal.aborted) {
          newest = mine;
          onFrame(bitmap);
        } else {
          bitmap.close();
        }
      }, function () {
        inflight -= 1;
      });
    }

    while (true) {
      var part = await reader.read();
      if (part.done) return;
      make(part.value.length);
      buffer.set(part.value, end);
      end += part.value.length;
      while (end - start >= 4) {
        var size = ((buffer[start] << 24) | (buffer[start + 1] << 16) | (buffer[start + 2] << 8) | buffer[start + 3]) >>> 0;
        if (end - start < 4 + size) break;
        var jpeg = buffer.slice(start + 4, start + 4 + size);
        start += 4 + size;
        if (inflight < MAX_INFLIGHT) decode(jpeg);
      }
    }
  }

  function freeSlot() {
    var taken = new Set(Array.from(shares.values(), function (share) { return share.slot; }));
    for (var n = 0; n < RTC.SLOTS; n += 1) {
      if (!taken.has(n)) return n;
    }
    return -1;
  }

  function shareInSlot(slot) {
    return Array.from(shares.values()).find(function (share) { return share.slot === slot; }) || null;
  }

  function postShare(share, sharing) {
    var body = sharing
      ? { slot: share.slot, sharing: true, label: share.label, width: share.width, height: share.height }
      : { slot: share.slot, sharing: false };
    post("/share", body).catch(function () {
      notice("The server did not hear about that screen. Check the connection to the host.");
    });
  }

  function stopShare(key) {
    var share = shares.get(key);
    if (!share) return;
    shares.delete(key);
    share.stop();
    share.track.stop();
    peers.forEach(function (entry) {
      RTC.setScreenTrack(entry.pc, share.slot, null).catch(function () {});
    });
    postShare(share, false);
    updateGoLive();
    renderMyShares();
    renderTiles();
  }

  // A frame share is captured at a size and rate chosen at Go Live, so a new choice means a new capture for it.
  function restartShare(key) {
    var old = shares.get(key);
    if (!old || !usesFrames()) return;
    old.stop();
    old.track.stop();
    var capture = frameCapture(old.opts || currentOptions(), key.slice("monitor-".length), sizeReporter(key), old.feedHeight);
    var share = Object.assign({}, old, { track: capture.track, stop: capture.stop, width: old.source ? old.width : 0, height: old.source ? old.height : 0 });
    share.track.addEventListener("ended", function () { stopShare(key); });
    shares.set(key, share);
    peers.forEach(function (entry, id) {
      attachShare(id, share);
    });
  }

  // The button shows the state of the screen picked in the menu, so picking another screen never flips it.
  function updateGoLive() {
    var key = selectedKey();
    var live = key !== null && shares.has(key);
    el.goLive.textContent = live ? "Stop sharing" : "Go Live";
    el.goLive.classList.toggle("live", live);
    el.liveSplit.classList.toggle("live", live);
    var can = Boolean(myVoice()) && !(me && me.role === "guest");
    el.liveSplit.classList.toggle("off", !can && !live);
    el.goLive.title = live ? "Stop sharing this screen" : can ? "Share your screen with the people in your voice channel"
      : me && me.role === "guest" ? "Guests watch and listen; they do not share screens" : "Join a voice channel to share your screen";
  }

  function renderMyShares() {
    el.myShares.textContent = "";
    shares.forEach(function (share) {
      var row = document.createElement("li");
      var name = document.createElement("span");
      name.className = "name";
      name.textContent = share.label;
      var stop = document.createElement("button");
      stop.className = "secondary small";
      stop.textContent = "Stop";
      stop.addEventListener("click", function () { stopShare(share.key); });
      row.append(name, stop);
      el.myShares.append(row);
    });
    el.mine.hidden = shares.size === 0;
  }

  function ceilingHeightOf(opts) {
    return opts.ceiling === "source" ? null : Number(opts.ceiling);
  }

  // The real size of a screen, from the Screen menu, or null when it is not known.
  function monitorSource(index) {
    var option = el.monitor.querySelector('option[value="' + index + '"]');
    var width = option ? Number(option.dataset.width) : 0;
    var height = option ? Number(option.dataset.height) : 0;
    return width > 0 && height > 0 ? { width: width, height: height } : null;
  }

  // A new quality ceiling starts the capture-size decision again from the ceiling.
  function resetCaptureSize(share) {
    if (!share.source) return;
    share.feedHeight = undefined;
    share.governor = new CaptureSize.Governor(ceilingHeightOf(share.opts || currentOptions()));
  }

  // Capture each shared screen at the size its viewers need. Everyone watching (and this window's own preview) says how big
  // the screen is on their side; the capture is the smallest standard size that covers the most demanding of them. A
  // larger need is met at once, a smaller one only after it has lasted, so the capture does not restart over a drag.
  function adaptCaptureSizes() {
    if (!me || !usesFrames()) return;
    var now = performance.now();
    Array.from(shares.keys()).forEach(function (key) {
      var share = shares.get(key);
      if (!share || !share.source || !share.governor) return;
      var qualities = [];
      peers.forEach(function (entry, id) {
        var member = members.get(id);
        if (member && member.online) qualities.push(entry.wants[share.slot]);
      });
      var own = tileKey(me.id, share.slot);
      var tile = tiles.get(own);
      if (tile && !hiddenKeys.has(own)) qualities.push(RTC.fitTo(tile.querySelector("video")));
      var ceilingHeight = ceilingHeightOf(share.opts || currentOptions());
      var need = CaptureSize.neededHeight(share.source, qualities, ceilingHeight);
      var decision = share.governor.decide(CaptureSize.stepFor(need, share.source.height), now);
      if (decision.changed) {
        share.feedHeight = decision.height;
        report("capture size now " + (decision.height === null ? "the screen's own" : decision.height + "p") + " for " + qualities.length + " viewer(s)");
        restartShare(key);
      }
    });
  }

  // A read-only look at what the capture-size decision sees, for the tests and for finding out why a capture is the size it is.
  window.PartyDebug = function () {
    return JSON.stringify(Array.from(shares.values()).map(function (share) {
      var viewers = [];
      peers.forEach(function (entry, id) {
        var member = members.get(id);
        viewers.push({ id: id, online: Boolean(member && member.online), wants: entry.wants[share.slot] || null });
      });
      return { key: share.key, slot: share.slot, source: share.source || null, feedHeight: share.feedHeight === undefined ? "unset" : share.feedHeight,
        governor: share.governor ? { current: share.governor.current, lowSince: share.governor.lowSince } : null, viewers: viewers };
    }));
  };

  // What the Go Live panel says now. Each share keeps its own copy, which its tile's settings can change.
  function currentOptions() {
    return { ceiling: el.ceiling.value, fps: Number(el.fps.value), pointer: el.pointer.value };
  }

  function ceilingFromChoices() {
    var height = el.ceiling.value === "source" ? null : Number(el.ceiling.value);
    return { height: height, fps: Number(el.fps.value) };
  }

  function applyGoLiveChoices() {
    savePref(PREFS.ceiling, el.ceiling.value);
    savePref(PREFS.fps, el.fps.value);
    savePref(PREFS.pointer, el.pointer.value);
    myCeiling = ceilingFromChoices();
    shares.forEach(function (share) {
      share.opts = currentOptions();
      resetCaptureSize(share);
    });
    if (usesFrames()) Array.from(shares.keys()).forEach(restartShare);
    shares.forEach(function (share) {
      peers.forEach(function (entry, id) {
        attachShare(id, share);
      });
    });
  }

  el.ceiling.addEventListener("change", applyGoLiveChoices);
  el.fps.addEventListener("change", applyGoLiveChoices);
  el.pointer.addEventListener("change", applyGoLiveChoices);

  function setSideHidden(hidden) {
    el.side.hidden = hidden;
    el.sideBtn.setAttribute("aria-pressed", String(hidden));
    el.sideBtn.title = hidden ? "Show the member list" : "Hide the member list";
    savePref(PREFS.side, hidden ? "hidden" : "shown");
  }

  el.sideBtn.addEventListener("click", function () {
    setSideHidden(!el.side.hidden);
  });

  // A new receive size means the tiles are sized for it again, so the grid arranges them afresh.
  el.receive.addEventListener("change", function () {
    savePref(PREFS.receive, el.receive.value);
    resetLayout();
    sendQualityToEveryone();
  });

  el.upscale.addEventListener("change", function () {
    savePref(PREFS.upscale, el.upscale.value);
    renderTiles();
  });

  el.resetLayout.addEventListener("click", resetLayout);
  el.showHidden.addEventListener("click", function () {
    Array.from(hiddenKeys).forEach(function (key) { setHidden(key, false); });
  });

  // --- tiles -------------------------------------------------------------------------------------------------

  function tileKey(id, slot) {
    return id + ":" + slot;
  }

  // A tile's size on screen decides what "Fit my window" asks the sender for, so a change is told to the sender, after
  // a short pause so dragging an edge does not send a request for every pixel.
  var observer = new ResizeObserver(function (entries) {
    entries.forEach(function (entry) {
      var member = entry.target.dataset.member;
      var key = entry.target.dataset.key;
      if (!member || (me && member === me.id) || receiveFor(key) !== "fit") return;
      clearTimeout(resizeTimers.get(key));
      resizeTimers.set(key, setTimeout(function () { sendQualityTo(member, Number(entry.target.dataset.slot)); }, 250));
    });
  });

  // The stage changing size re-arranges an arranged grid and pulls placed tiles back inside.
  new ResizeObserver(function () {
    if (!focusKey) applyGridLayout();
  }).observe(el.tiles);

  function renderTiles() {
    if (!me) return;
    var wanted = new Map();
    members.forEach(function (member) {
      var entry = member.id === me.id ? null : peers.get(member.id);
      if (!entry || !sameVoice(member.id)) return; // screens from other channels are not shown here
      sharesOf(member.id).forEach(function (share) {
        var track = RTC.remoteTrack(entry.pc, share.slot);
        if (!track) return;
        wanted.set(tileKey(member.id, share.slot), {
          member: member.id, slot: share.slot, name: member.name + " - " + share.label,
          width: share.width, height: share.height, track: track,
        });
      });
    });
    shares.forEach(function (share) {
      wanted.set(tileKey(me.id, share.slot), {
        member: me.id, slot: share.slot, name: "You - " + share.label,
        width: share.width, height: share.height, track: share.track,
      });
    });

    Array.from(tiles.keys()).forEach(function (key) {
      if (!wanted.has(key)) removeTile(key);
    });
    wanted.forEach(function (item, key) {
      var isNew = !tiles.has(key);
      var tile = tiles.get(key) || addTile(key, item);
      if (isNew) animateNext = true;
      tileInfo.set(key, item);
      var cached = streams.get(key);
      if (!cached || cached.track !== item.track) {
        cached = { track: item.track, stream: new MediaStream([item.track]) };
        streams.set(key, cached);
      }
      var video = tile.querySelector("video");
      if (video.srcObject !== cached.stream) video.srcObject = cached.stream;
      tile.querySelector(".label").textContent = item.name;
      applyHidden(tile, key);
    });
    el.empty.hidden = wanted.size > 0 && shownView.channel === myVoice();
    layoutTiles();
    tiles.forEach(function (tile, key) {
      setUpscaled(tile, wantsUpscale(tile, key));
    });
  }

  function addTile(key, item) {
    var tile = document.createElement("div");
    tile.className = "tile";
    tile.dataset.key = key;
    tile.dataset.member = item.member;
    tile.dataset.slot = String(item.slot);
    var frame = document.createElement("div");
    frame.className = "frame";
    var video = document.createElement("video");
    video.autoplay = true;
    video.playsInline = true;
    video.muted = true; // screens carry no sound
    var note = document.createElement("div");
    note.className = "hidden-note";
    var message = document.createElement("span");
    message.textContent = "This stream is hidden.";
    var show = document.createElement("button");
    show.textContent = "Click here to show it again";
    show.addEventListener("click", function (event) {
      event.stopPropagation();
      setHidden(key, false);
    });
    note.append(message, show);
    frame.append(video, note);
    var label = document.createElement("span");
    label.className = "label";
    tile.append(frame, label);
    var tools = document.createElement("div");
    tools.className = "tools";
    tools.append(toolButton("gear", "Settings for this stream", "tool settings", function () { toggleStreamPanel(key); }));
    tools.append(toolButton("expand", "Fullscreen this stream", "tool fullscreen", function () { toggleTileFullscreen(key); }));
    tile.append(tools);
    EDGES.forEach(function (edge) {
      var handle = document.createElement("i");
      handle.className = "handle";
      handle.dataset.edge = edge;
      tile.append(handle);
    });
    tile.addEventListener("pointerdown", onTilePointerDown);
    tile.addEventListener("contextmenu", function (event) {
      event.preventDefault();
      openMenu(key, event.clientX, event.clientY);
    });
    el.tiles.append(tile);
    tiles.set(key, tile);
    observer.observe(tile);
    return tile;
  }

  function removeTile(key) {
    var tile = tiles.get(key);
    if (tile) {
      observer.unobserve(tile);
      tile.remove();
    }
    tiles.delete(key);
    tileInfo.delete(key);
    streams.delete(key);
    rects.delete(key);
    hiddenKeys.delete(key);
    streamPrefs.delete(key);
    if (streamPanel && streamPanel.key === key) streamPanel = null;
    if (focusKey === key) focusKey = null;
    if (tileFull === key) {
      tileFull = null;
      setTimeout(syncFullscreen, 0); // after this tile is gone from the list
    }
    animateNext = true;
  }

  // Put every tile where it belongs: in the grid, or in the focus view's big area and preview strip.
  function layoutTiles() {
    if (focusKey && !tiles.has(focusKey)) focusKey = null;
    var focusing = Boolean(focusKey);
    el.tiles.hidden = focusing;
    el.focusView.hidden = !focusing;
    el.stage.classList.toggle("focusing", focusing);
    tiles.forEach(function (tile, key) {
      var host = focusing ? (key === focusKey ? el.primary : el.strip) : el.tiles;
      if (tile.parentElement !== host) {
        host.append(tile);
        var video = tile.querySelector("video");
        if (!tile.classList.contains("stream-hidden")) video.play().catch(function () {});
      }
      tile.classList.toggle("in-primary", focusing && key === focusKey);
      tile.classList.toggle("in-strip", focusing && key !== focusKey);
      if (focusing) {
        tile.style.left = tile.style.top = tile.style.width = tile.style.height = "";
        tile.classList.remove("dragging");
      }
    });
    el.strip.hidden = !(focusing && tiles.size > 1);
    if (!focusing) applyGridLayout();
  }

  function stageSize() {
    return { w: el.tiles.clientWidth, h: el.tiles.clientHeight };
  }

  function itemFor(key) {
    var info = tileInfo.get(key) || {};
    var item = { key: key, aspect: Layout.aspectOf(info) };
    var percent = Number(receiveFor(key));
    if (receiveFor(key) !== "fit" && me && info.member !== me.id && info.width > 0 && info.height > 0) {
      item.maxW = (info.width * percent) / 100;
      item.maxH = (info.height * percent) / 100;
    }
    return item;
  }

  // Arrange the grid. While nobody has placed a tile, the grid arranges them all. Once someone has, each tile keeps
  // its place, a new tile takes a free spot, and a smaller stage pulls tiles back inside.
  function applyGridLayout() {
    var stage = stageSize();
    if (stage.w < 60 || stage.h < 60) return;
    var keys = Array.from(tiles.keys());
    if (!layoutManual) {
      Layout.autoArrange(keys.map(itemFor), stage).forEach(function (r) {
        rects.set(r.key, { x: r.x, y: r.y, w: r.w, h: r.h });
      });
    } else {
      keys.forEach(function (key) {
        var existing = rects.get(key);
        if (existing) {
          rects.set(key, Layout.clampInto(existing, stage, Math.min(existing.w, Layout.MIN_W), Math.min(existing.h, Layout.MIN_H)));
        } else {
          var size = Layout.defaultSize(Layout.aspectOf(tileInfo.get(key)), stage);
          var others = Array.from(rects.values());
          rects.set(key, Layout.findFreeSpot(size, others, stage));
        }
      });
    }
    Array.from(rects.keys()).forEach(function (key) {
      if (!tiles.has(key)) rects.delete(key);
    });
    var animate = animateNext && !press;
    animateNext = false;
    tiles.forEach(function (tile, key) {
      if (animate) tile.classList.add("animate");
      paintRect(tile, rects.get(key));
    });
    if (animate) {
      setTimeout(function () {
        tiles.forEach(function (tile) { tile.classList.remove("animate"); });
      }, 260);
    }
  }

  function paintRect(tile, r) {
    if (!r) return;
    tile.style.left = r.x + "px";
    tile.style.top = r.y + "px";
    tile.style.width = r.w + "px";
    tile.style.height = r.h + "px";
  }

  function setRect(key, r) {
    var next = { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h) };
    rects.set(key, next);
    var tile = tiles.get(key);
    if (tile) paintRect(tile, next);
  }

  function otherRects(key) {
    var out = [];
    rects.forEach(function (r, k) {
      if (k !== key && tiles.has(k)) out.push(r);
    });
    return out;
  }

  function resetLayout() {
    layoutManual = false;
    rects.clear();
    focusKey = null;
    animateNext = true;
    renderTiles();
  }

  function setFocus(key) {
    if (!tiles.has(key)) return;
    focusKey = key;
    layoutTiles();
    tiles.forEach(function (tile, k) {
      setUpscaled(tile, wantsUpscale(tile, k));
    });
  }

  function exitFocus() {
    focusKey = null;
    animateNext = true;
    layoutTiles();
    tiles.forEach(function (tile, k) {
      setUpscaled(tile, wantsUpscale(tile, k));
    });
  }

  // --- moving, resizing and clicking a tile ----------------------------------------------------------------

  function onTilePointerDown(event) {
    if (event.button !== 0 || document.fullscreenElement) return;
    if (event.target.closest("button, .tools, .stream-panel")) return; // a tile's own controls handle themselves
    var tile = event.currentTarget;
    var key = tile.dataset.key;
    var handle = event.target.closest(".handle");
    closeMenu();
    closePopovers();
    hideTip();
    var start = rects.get(key);
    press = {
      key: key, tile: tile, x: event.clientX, y: event.clientY, moved: false,
      edge: handle ? handle.dataset.edge : null,
      rect0: start ? { x: start.x, y: start.y, w: start.w, h: start.h } : null,
    };
    tile.style.zIndex = String((zTop += 1));
    document.addEventListener("pointermove", onPressMove);
    document.addEventListener("pointerup", onPressEnd);
    document.addEventListener("pointercancel", onPressEnd);
    event.preventDefault();
  }

  function onPressMove(event) {
    if (!press) return;
    var dx = event.clientX - press.x;
    var dy = event.clientY - press.y;
    if (!press.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    if (focusKey || !press.rect0) return; // tiles do not move in the focus view; letting go still counts as a click
    if (!press.moved) {
      press.moved = true;
      layoutManual = true;
      press.tile.classList.add("dragging");
      press.tile.classList.remove("animate");
    }
    var stage = stageSize();
    var others = otherRects(press.key);
    var r0 = press.rect0;
    if (press.edge) {
      var aspect = event.shiftKey ? 0 : r0.w / r0.h; // hold Shift to resize without keeping the picture's shape
      var result = Layout.resize(r0, press.edge, dx, dy, others, stage, { aspect: aspect });
      setRect(press.key, result.rect);
      showGuides(result.guides);
    } else {
      var moved = Layout.clampInto({ x: r0.x + dx, y: r0.y + dy, w: r0.w, h: r0.h }, stage, r0.w, r0.h);
      var snapped = Layout.snapMove(moved, others, stage);
      setRect(press.key, { x: snapped.x, y: snapped.y, w: r0.w, h: r0.h });
      showGuides(snapped.guides);
    }
  }

  function onPressEnd(event) {
    document.removeEventListener("pointermove", onPressMove);
    document.removeEventListener("pointerup", onPressEnd);
    document.removeEventListener("pointercancel", onPressEnd);
    var finished = press;
    press = null;
    if (!finished) return;
    hideGuides();
    finished.tile.classList.remove("dragging");
    if (!finished.moved && event.type === "pointerup") handleTileClick(finished.key);
  }

  // A click makes a screen big. A click on the big screen, or on a preview, does the sensible thing from there.
  function handleTileClick(key) {
    if (focusKey === key) exitFocus();
    else setFocus(key);
  }

  function showGuides(guides) {
    el.guides.textContent = "";
    var stage = stageSize();
    guides.forEach(function (g) {
      var line = document.createElement("div");
      line.className = "guide " + g.axis;
      if (g.axis === "v") {
        var to = g.to == null ? stage.h : g.to;
        line.style.left = Math.round(g.pos) + "px";
        line.style.top = Math.round(g.from) + "px";
        line.style.height = Math.max(1, Math.round(to - g.from)) + "px";
      } else {
        var right = g.to == null ? stage.w : g.to;
        line.style.top = Math.round(g.pos) + "px";
        line.style.left = Math.round(g.from) + "px";
        line.style.width = Math.max(1, Math.round(right - g.from)) + "px";
      }
      el.guides.append(line);
    });
  }

  function hideGuides() {
    el.guides.textContent = "";
  }

  // --- hiding a stream and the right-click menu -----------------------------------------------------------

  function applyHidden(tile, key) {
    var on = hiddenKeys.has(key);
    if (tile.classList.contains("stream-hidden") === on) return;
    tile.classList.toggle("stream-hidden", on);
    var video = tile.querySelector("video");
    if (on) video.pause();
    else video.play().catch(function () {});
  }

  // Hiding a stream shows a black screen with a way back, and tells the sender to stop sending it to you. Your own
  // screen can be hidden too, which only hides your preview of it.
  function setHidden(key, on) {
    if (on) hiddenKeys.add(key);
    else hiddenKeys.delete(key);
    var tile = tiles.get(key);
    if (tile) applyHidden(tile, key);
    var item = tileInfo.get(key);
    if (item && me && item.member !== me.id) sendQualityTo(item.member, item.slot);
  }

  function closeMenu() {
    var was = !el.ctxMenu.hidden;
    el.ctxMenu.hidden = true;
    el.ctxMenu.textContent = "";
    return was;
  }

  // What the owner or an admin may do to one member. The owner can make admins; an admin can only move members and guests.
  function memberMenu(member) {
    var entries = [];
    if (!me || member.id === me.id || member.id === hostId) return entries;
    var owner = me.role === "host";
    var admin = me.role === "admin";
    if (!owner && !admin) return entries;
    if (admin && member.role === "admin") return entries;
    function setRole(role) {
      return function () { post("/role", { id: member.id, role: role }).catch(function () { notice("That role change was refused."); }); };
    }
    if (owner && member.role !== "admin") entries.push({ label: "Make admin", run: setRole("admin") });
    if (owner && member.role === "admin") entries.push({ label: "Make member", run: setRole("member") });
    if (member.role === "guest") entries.push({ label: "Let them talk and post (member)", run: setRole("member") });
    if (member.role === "member") entries.push({ label: "Make guest (watch and read only)", run: setRole("guest") });
    entries.push("-");
    entries.push({ label: "Remove from the server", danger: true, run: function () { post("/kick", { id: member.id }).catch(function () {}); } });
    if (member.user) {
      entries.push({ label: "Ban", danger: true, run: function () { post("/ban", { id: member.id }).catch(function () { notice("That ban was refused."); }); } });
    }
    return entries;
  }

  function openMenu(key, x, y) {
    var item = tileInfo.get(key);
    if (!item || !me) return;
    closePopovers();
    hideTip();
    var mine = item.member === me.id;
    var hidden = hiddenKeys.has(key);
    var entries = [];
    entries.push(focusKey === key
      ? { label: "Back to the grid", run: exitFocus }
      : { label: "Make this stream big", run: function () { setFocus(key); } });
    entries.push({ label: document.fullscreenElement === tiles.get(key) || tileFull === key ? "Leave fullscreen" : "Fullscreen this stream", run: function () { toggleTileFullscreen(key); } });
    entries.push({ label: "Settings for this stream", run: function () { toggleStreamPanel(key); } });
    entries.push({ label: hidden ? "Show this stream" : "Don't show this stream", danger: !hidden, run: function () { setHidden(key, !hidden); } });
    if (!focusKey) {
      entries.push("-");
      entries.push({ label: "Reset layout", run: resetLayout });
    }
    showMenu(entries, x, y);
  }

  function showMenu(entries, x, y) {
    closePopovers();
    el.ctxMenu.textContent = "";
    entries.forEach(function (entry) {
      if (entry === "-") {
        el.ctxMenu.append(document.createElement("hr"));
        return;
      }
      var button = document.createElement("button");
      button.textContent = entry.label;
      button.setAttribute("role", "menuitem");
      if (entry.danger) button.className = "danger";
      button.addEventListener("click", function (event) {
        event.stopPropagation();
        closeMenu();
        entry.run();
      });
      el.ctxMenu.append(button);
    });
    el.ctxMenu.hidden = false;
    var box = el.ctxMenu.getBoundingClientRect();
    el.ctxMenu.style.left = Math.max(4, Math.min(x, window.innerWidth - box.width - 4)) + "px";
    el.ctxMenu.style.top = Math.max(4, Math.min(y, window.innerHeight - box.height - 4)) + "px";
  }

  // --- one stream's own controls --------------------------------------------------------------------------

  // Each stream's tile carries its own settings, fullscreen and pop-out buttons, so it is always clear which stream a
  // control affects. The View panel holds the defaults; a stream's own choice wins over them.
  function receiveFor(key) {
    var own = streamPrefs.get(key);
    return (own && own.receive) || el.receive.value;
  }

  function upscaleFor(key) {
    var own = streamPrefs.get(key);
    return (own && own.upscale) || el.upscale.value;
  }

  function upscaleInUse() {
    return el.upscale.value === "on" || Array.from(streamPrefs.values()).some(function (own) { return own.upscale === "on"; });
  }

  function wantsUpscale(tile, key) {
    var item = tileInfo.get(key);
    return Boolean(item && me) && item.member !== me.id && upscaleFor(key) === "on" && !tile.classList.contains("in-strip");
  }

  function toolButton(name, title, className, run) {
    var button = document.createElement("button");
    button.className = className;
    button.title = title;
    button.setAttribute("aria-label", title);
    button.append(icon(name));
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      run();
    });
    return button;
  }

  function selectRow(label, options, value, onChange) {
    var row = document.createElement("label");
    row.className = "row";
    var text = document.createElement("span");
    text.textContent = label;
    var select = document.createElement("select");
    options.forEach(function (option) {
      var node = document.createElement("option");
      node.value = option[0];
      node.textContent = option[1];
      select.append(node);
    });
    select.value = value;
    select.addEventListener("change", function () { onChange(select.value); });
    row.append(text, select);
    return row;
  }

  function closeStreamPanel() {
    if (!streamPanel) return false;
    streamPanel.node.remove();
    streamPanel = null;
    return true;
  }

  function toggleStreamPanel(key) {
    var again = Boolean(streamPanel && streamPanel.key === key);
    closePopovers();
    closeMenu();
    hideTip();
    if (!again) openStreamPanel(key);
  }

  function openStreamPanel(key) {
    var tile = tiles.get(key);
    var item = tileInfo.get(key);
    if (!tile || !item || !me) return;
    var node = document.createElement("div");
    node.className = "stream-panel popover";
    node.setAttribute("role", "dialog");
    node.addEventListener("click", function (event) { event.stopPropagation(); });
    var title = document.createElement("h3");
    title.textContent = item.name;
    node.append(title);
    var note = document.createElement("p");
    note.className = "muted";
    if (item.member === me.id) {
      var share = shareInSlot(item.slot);
      if (!share) return;
      var opts = share.opts || currentOptions();
      node.append(
        selectRow("Quality", [["source", "Source"], ["1080", "1080p"], ["720", "720p"], ["480", "480p"]], opts.ceiling,
          function (value) { changeShareOptions(item.slot, { ceiling: value }); }),
        selectRow("Frame rate", [["15", "15 fps"], ["30", "30 fps"], ["60", "60 fps"]], String(opts.fps),
          function (value) { changeShareOptions(item.slot, { fps: Number(value) }); }),
        selectRow("Mouse pointer", [["on", "Shown"], ["off", "Hidden"]], opts.pointer,
          function (value) { changeShareOptions(item.slot, { pointer: value }); })
      );
      note.textContent = "These apply to this screen only, for everyone watching it.";
    } else {
      var own = streamPrefs.get(key) || {};
      node.append(
        selectRow("Receive size", [["", "Default (View panel)"], ["fit", "Fit this window"], ["100", "100% of the sender's size"],
          ["75", "75%"], ["50", "50%"], ["25", "25%"]], own.receive || "",
          function (value) { setStreamPref(key, "receive", value); }),
        selectRow("Upscale", [["", "Default (View panel)"], ["off", "Off"], ["on", "On (smooth)"]], own.upscale || "",
          function (value) { setStreamPref(key, "upscale", value); })
      );
      note.textContent = "These apply to this stream only.";
    }
    node.append(note);
    tile.append(node);
    placeStreamPanel(node, tile.querySelector(".tool.settings") || tile);
    streamPanel = { key: key, node: node };
  }

  // The panel is fixed to the window, under its button where it fits and above it where it does not, so a small tile near
  // an edge never clips it (the stage cuts off anything that is positioned inside it).
  function placeStreamPanel(node, anchor) {
    var gap = 8;
    var box = anchor.getBoundingClientRect();
    var width = node.offsetWidth;
    var height = node.offsetHeight;
    var left = Math.min(Math.max(gap, box.right - width), Math.max(gap, window.innerWidth - width - gap));
    var top = box.bottom + 6;
    if (top + height > window.innerHeight - gap) top = Math.min(box.top - height - 6, window.innerHeight - height - gap);
    node.style.left = left + "px";
    node.style.top = Math.max(gap, top) + "px";
  }

  function setStreamPref(key, name, value) {
    var own = Object.assign({}, streamPrefs.get(key));
    if (value) own[name] = value;
    else delete own[name];
    if (Object.keys(own).length) streamPrefs.set(key, own);
    else streamPrefs.delete(key);
    var item = tileInfo.get(key);
    if (name === "receive") {
      if (item && me && item.member !== me.id) sendQualityTo(item.member, item.slot);
      animateNext = true;
    }
    renderTiles();
  }

  // Quality, frame rate and pointer for one of this page's own screens.
  function changeShareOptions(slot, patch) {
    var share = shareInSlot(slot);
    if (!share) return;
    share.opts = Object.assign({}, share.opts || currentOptions(), patch);
    resetCaptureSize(share);
    if (usesFrames()) {
      restartShare(share.key);
    } else {
      peers.forEach(function (entry, id) {
        attachShare(id, share);
      });
    }
    renderTiles();
  }

  // Fullscreen belongs to the stream whose button was pressed. Inside the PrivatePartyNet app the window host ignores the browser's fullscreen request (an element would only fill the page inside the
  // window's frame), so the app is asked to make the window itself cover its display, and the stream fills the window.
  // In a plain browser the browser's own fullscreen is used.
  function appWindow() {
    var api = window.pywebview && window.pywebview.api;
    return api && api.set_fullscreen ? api : null;
  }

  var windowFull = false; // the top bar's Fullscreen: the whole window covers the display
  var tileFull = null; // the key of the stream shown fullscreen by the app's window

  function syncFullscreen() {
    var api = appWindow();
    if (api) api.set_fullscreen(windowFull || tileFull !== null).catch(function () {});
    document.body.classList.toggle("tile-fullscreen", tileFull !== null);
    tiles.forEach(function (tile, key) {
      var html = document.fullscreenElement === tile;
      var app = tileFull === key;
      tile.classList.toggle("is-fullscreen", html);
      tile.classList.toggle("in-window-fullscreen", app);
      var button = tile.querySelector(".tool.fullscreen");
      if (!button) return;
      button.title = html || app ? "Leave fullscreen" : "Fullscreen this stream";
      button.setAttribute("aria-label", button.title);
      button.replaceChildren(icon(html || app ? "shrink" : "expand"));
    });
    el.statsbar.hidden = windowFull || document.fullscreenElement === document.documentElement;
  }

  function toggleTileFullscreen(key) {
    var tile = tiles.get(key);
    if (!tile) return;
    closePopovers();
    closeMenu();
    if (appWindow()) {
      tileFull = tileFull === key ? null : key;
      syncFullscreen();
      return;
    }
    if (document.fullscreenElement === tile) {
      document.exitFullscreen().catch(function () {});
      return;
    }
    var enter = function () {
      tile.requestFullscreen().catch(function () {
        notice("Fullscreen is not available for that screen.");
      });
    };
    if (document.fullscreenElement) document.exitFullscreen().then(enter, enter);
    else enter();
  }

  document.addEventListener("fullscreenchange", syncFullscreen);
  window.addEventListener("pywebviewready", syncFullscreen); // a reloaded page learns the window's state again

  // This person's own accent, text size and spacing from Settings, kept on this PC. Read again whenever the window comes
  // to the front, so a change made in the control panel shows here too. A page opened without the app keeps the default.
  function syncAppearance() {
    var api = window.pywebview && window.pywebview.api;
    if (!api || !api.appearance || !window.PPNAppearance) return;
    api.appearance().then(function (look) {
      if (look) window.PPNAppearance.apply(document, look);
    }).catch(function () { /* the next focus tries again */ });
  }
  window.addEventListener("pywebviewready", syncAppearance);
  window.addEventListener("focus", syncAppearance);

  el.full.addEventListener("click", function () {
    if (appWindow()) {
      windowFull = !windowFull;
      if (!windowFull) tileFull = null;
      syncFullscreen();
      return;
    }
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen();
  });

  // --- the top bar's panels --------------------------------------------------------------------------------

  var popovers = [
    { button: el.liveSettings, panel: el.livePanel },
    { button: el.viewBtn, panel: el.viewPanel },
  ];

  function closePopovers() {
    var was = closeStreamPanel();
    popovers.forEach(function (p) {
      if (!p.panel.hidden) was = true;
      p.panel.hidden = true;
      p.button.setAttribute("aria-expanded", "false");
    });
    return was;
  }

  popovers.forEach(function (p) {
    p.button.addEventListener("click", function (event) {
      event.stopPropagation();
      var opening = p.panel.hidden;
      closePopovers();
      closeMenu();
      hideTip();
      if (!opening) return;
      p.panel.hidden = false;
      p.button.setAttribute("aria-expanded", "true");
      var box = p.button.getBoundingClientRect();
      p.panel.style.left = "auto";
      p.panel.style.right = Math.max(8, window.innerWidth - box.right) + "px";
    });
    p.panel.addEventListener("click", function (event) { event.stopPropagation(); });
  });

  document.addEventListener("click", function () {
    closePopovers();
    closeMenu();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var closed = closePopovers();
    closed = closeMenu() || closed;
    if (closed) return;
    if (tileFull !== null) {
      tileFull = null;
      syncFullscreen();
    } else if (windowFull) {
      windowFull = false;
      syncFullscreen();
    } else if (focusKey) {
      exitFocus();
    }
  });

  // --- upscaling ------------------------------------------------------------------------------------------

  // Off: the browser scales the video to the tile, on the GPU. On: each frame is drawn into a canvas the size of the
  // tile with high-quality smoothing, which runs on the CPU. The video stays decoding behind it.
  function setUpscaled(tile, on) {
    var video = tile.querySelector("video");
    var frame = tile.querySelector(".frame");
    var canvas = tile.querySelector("canvas.upscaled");
    if (on && !canvas) {
      canvas = document.createElement("canvas");
      canvas.className = "upscaled";
      frame.insertBefore(canvas, tile.querySelector(".hidden-note"));
    }
    if (canvas) canvas.hidden = !on;
    video.classList.toggle("behind", on);
    el.upscaleNote.hidden = !upscaleInUse();
    if (on) startUpscale();
  }

  function startUpscale() {
    if (upscaleRunning) return;
    upscaleRunning = true;
    requestAnimationFrame(drawUpscaled);
  }

  function drawUpscaled() {
    var any = false;
    tiles.forEach(function (tile) {
      var canvas = tile.querySelector("canvas.upscaled");
      if (!canvas || canvas.hidden || tile.classList.contains("stream-hidden")) return;
      any = true;
      var began = performance.now();
      drawFrame(tile.querySelector("video"), canvas);
      upscaleBusyMs += performance.now() - began;
    });
    if (any) {
      requestAnimationFrame(drawUpscaled);
    } else {
      upscaleRunning = false;
    }
  }

  function drawFrame(video, canvas) {
    if (!video.videoWidth) return;
    var ratio = window.devicePixelRatio || 1;
    var width = Math.round(canvas.clientWidth * ratio);
    var height = Math.round(canvas.clientHeight * ratio);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    var scale = Math.min(width / video.videoWidth, height / video.videoHeight);
    var drawWidth = Math.round(video.videoWidth * scale);
    var drawHeight = Math.round(video.videoHeight * scale);
    var ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.fillStyle = "#000000";
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(video, Math.round((width - drawWidth) / 2), Math.round((height - drawHeight) / 2), drawWidth, drawHeight);
  }

  // --- the roster ------------------------------------------------------------------------------------------

  function hueOf(text) {
    var h = 0;
    for (var i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) % 360;
    return h;
  }

  // The picture a person gave this server (Settings, My profile, in their app), by member id or permanent user id.
  function pictureOf(id) {
    var member = members.get(id);
    if (!member) members.forEach(function (m) { if (!member && m.user && m.user === id) member = m; });
    var hash = member && member.avatar;
    return typeof hash === "string" && /^[0-9a-f]{64}$/.test(hash) ? hash : null;
  }

  // A round badge with the person's picture, or the first letter of their name in a color taken from them, as Discord does.
  function avatar(name, id) {
    var badge = document.createElement("span");
    badge.className = "avatar";
    var letter = (name || "?").trim().charAt(0).toUpperCase() || "?";
    badge.textContent = letter;
    badge.style.background = "hsl(" + hueOf(String(id || name)) + " 45% 42%)";
    var hash = pictureOf(id);
    if (hash) {
      var img = document.createElement("img");
      img.alt = "";
      img.src = "/blob?hash=" + hash;
      img.addEventListener("error", function () { img.remove(); badge.textContent = letter; badge.classList.remove("has-picture"); });
      badge.textContent = "";
      badge.classList.add("has-picture");
      badge.append(img);
    }
    return badge;
  }

  // Pictures changed in the roster: the messages already shown get them too.
  var pictureKey = "";
  function picturesChanged() {
    var key = Array.from(members.values()).map(function (m) { return (m.user || m.id) + "=" + (m.avatar || ""); }).sort().join(",");
    if (key === pictureKey) return;
    pictureKey = key;
    chat.refresh();
  }

  function renderRoster() {
    el.roster.textContent = "";
    // People who are here come first, then the ones who are away, each under a small heading with a count.
    var people = Array.from(members.values());
    var groups = [
      { title: "Online", list: people.filter(function (m) { return m.online; }) },
      { title: "Offline", list: people.filter(function (m) { return !m.online; }) },
    ];
    var panels = window.PPNPanels;
    groups.forEach(function (group) {
      if (!group.list.length) return;
      // each group's heading folds it away; remembered on this PC (panels.js)
      var key = "room.members." + group.title.toLowerCase();
      var folded = Boolean(panels) && panels.read(window.localStorage, key) === "folded";
      var heading = document.createElement("li");
      heading.className = "group" + (folded ? " folded" : "");
      var fold = document.createElement("button");
      fold.type = "button";
      fold.className = "fold";
      fold.setAttribute("aria-expanded", String(!folded));
      var chevron = document.createElement("span");
      chevron.className = "chev";
      fold.append(chevron, document.createTextNode(group.title + ", " + group.list.length));
      fold.addEventListener("click", function () {
        if (panels) panels.write(window.localStorage, key, folded ? null : "folded");
        renderRoster();
      });
      heading.append(fold);
      el.roster.append(heading);
      if (!folded) group.list.forEach(addRosterRow);
    });
  }

  function addRosterRow(member) {
    {  // one person's row: avatar with presence, name, role, voice and live badges, and the actions button
      var row = document.createElement("li");
      row.className = member.online ? "online" : "offline";
      row.dataset.id = member.id;
      if (voice && voice.speaking().has(member.id)) row.classList.add("speaking");
      var wrap = document.createElement("span");
      wrap.className = "avwrap";
      var presence = document.createElement("span");
      presence.className = "presence";
      wrap.append(avatar(member.name, member.id), presence);
      var name = document.createElement("span");
      name.className = "name";
      name.textContent = member.name + (member.id === me.id ? " (you)" : "");
      row.append(wrap, name);
      var roleLabel = member.id === hostId ? "Owner" : member.role === "admin" ? "Admin" : member.role === "guest" ? "Guest" : "";
      if (roleLabel) {
        var badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = roleLabel;
        row.append(badge);
      }
      if (member.voice) {
        var inVoice = document.createElement("span");
        inVoice.className = "badge voice";
        inVoice.textContent = "Voice";
        row.append(inVoice);
      }
      var count = sharesOf(member.id).length;
      if (count) {
        var live = document.createElement("span");
        live.className = "live";
        live.textContent = count > 1 ? "LIVE " + count : "LIVE";
        row.append(live);
      }
      var entries = memberMenu(member);
      if (entries.length) {
        var more = document.createElement("button");
        more.className = "secondary small";
        more.textContent = "...";
        more.title = "Roles and removal";
        more.setAttribute("aria-label", "Actions for " + member.name);
        more.addEventListener("click", function (event) {
          event.stopPropagation();
          var box = more.getBoundingClientRect();
          showMenu(entries, box.left, box.bottom + 2);
        });
        row.append(more);
      }
      el.roster.append(row);
    }
  }

  function updateCount() {
    var online = Array.from(members.values()).filter(function (m) { return m.online; }).length;
    el.count.textContent = online + (online === 1 ? " person online" : " people online");
  }

  // --- chat and name ----------------------------------------------------------------------------------------

  var chat = PartyChat.create({
    el: { messages: el.messages, chatForm: el.chatForm, chatText: el.chatText, chatTitle: el.chatTitle,
          channelList: el.channelList, addText: el.addText, addVoice: el.addVoice,
          lightbox: el.lightbox, dropZone: document.body },
    post: post,
    upload: uploadBlob,
    uploadFile: uploadFile,
    keep: keepChat(),
    keepBlob: keepPicture,
    getJson: getJson,
    notice: notice,
    avatar: avatar,
    isHidden: function () { return document.hidden || !shellShown || shownView.kind !== "text"; },
    // a message from someone else, in a channel that is not muted, while this person is not looking at it
    onArrive: function (msg, isMutedChannel) {
      if (isMutedChannel || !me || msg.from === me.id) return;
      if (!shellShown || document.hidden || !document.hasFocus() || msg.channel !== chat.current()) shellSound("message");
    },
    onVoice: function (channelId, channelName) {
      if (channelId) {
        voice.join(channelId, channelName, Boolean(me && me.role === "guest"));
        chat.view(channelId); // joining shows the channel, where its screens are
      } else {
        voice.leave();
      }
    },
    onSelect: function () { showView("text"); },
    onView: function (channelId) { showView("voice", channelId); },
  });

  // --- which channel the middle shows ------------------------------------------------------------------------

  var shownView = { kind: "text", channel: null };

  function showView(kind, channelId) {
    shownView = { kind: kind, channel: kind === "voice" ? channelId : null };
    document.body.dataset.view = kind;
    el.chat.hidden = kind !== "text";
    el.stage.hidden = kind !== "voice";
    if (kind === "text") chat.seen();
    renderView();
    renderTiles();
    sendQualityToEveryone(); // screens are received only while someone here looks at them
  }

  function renderView() {
    if (!me) return;
    var voiceView = shownView.kind === "voice";
    var id = voiceView ? shownView.channel : chat.current();
    var name = chat.channelName(id) || "";
    el.titleText.textContent = name;
    var use = el.titleIcon.querySelector("use");
    if (use) use.setAttribute("href", voiceView ? "#i-speaker" : "#i-hash");
    el.titleNote.textContent = "";
    if (!voiceView) return;
    var inHere = myVoice() === id;
    var guest = me.role === "guest";
    var people = chat.occupants(id);
    el.titleNote.textContent = people.length ? people.length + (people.length === 1 ? " person in voice" : " people in voice") : "";
    el.stage.classList.toggle("elsewhere", !inHere);
    el.liveSplit.hidden = !inHere || guest;
    if (inHere) {
      el.emptyTitle.textContent = "Nobody is live yet.";
      el.emptyText.textContent = guest ? "Screens shared in " + name + " show here." : "Press Go Live to share your screen with the people in " + name + ".";
    } else {
      el.emptyTitle.textContent = myVoice() ? "You are in " + chat.channelName(myVoice()) + "." : people.length ? name : name + " is empty.";
      el.emptyText.textContent = (guest ? "Guests join to watch and listen; they do not talk or share. " : "") +
        "Screens shared in a voice channel show to the people in it.";
    }
    el.voiceJoin.hidden = inHere;
    el.voiceJoin.textContent = myVoice() ? "Move to " + name : guest ? "Join to watch" : "Join voice";
    el.voiceRoom.hidden = inHere && tiles.size > 0;
    // drawn again only when who is there changes, not on every message the room receives
    var roomKey = id + "|" + people.map(function (p) { return [p.id, p.name, p.avatar || "", sharesOf(p.id).length].join(","); }).join(";");
    if (roomKey === lastRoomKey) return;
    lastRoomKey = roomKey;
    el.voiceRoom.textContent = "";
    people.forEach(function (person) {
      var li = document.createElement("li");
      li.dataset.id = person.id;
      li.append(avatar(person.name, person.id));
      var who = document.createElement("span");
      who.textContent = person.name + (person.id === me.id ? " (you)" : "");
      li.append(who);
      if (sharesOf(person.id).length) {
        var live = document.createElement("span");
        live.className = "live";
        live.textContent = "LIVE";
        li.append(live);
      }
      el.voiceRoom.append(li);
    });
  }
  var lastRoomKey = "";

  el.voiceJoin.addEventListener("click", function () {
    var id = shownView.channel;
    if (!id) return;
    voice.join(id, chat.channelName(id), Boolean(me && me.role === "guest"));
  });

  var voice = PartyVoice.create({
    rtc: RTC,
    peers: function () { return peers; },
    members: function () { return members; },
    meId: function () { return me && me.id; },
    post: post,
    notice: notice,
    onSound: shellSound,
    onSpeaking: function (who) {
      // The speaking ring shows in the members list and under the voice channel in the channel list.
      document.querySelectorAll("#roster li[data-id], #channelList .voice-people li[data-id]").forEach(function (row) {
        row.classList.toggle("speaking", who.has(row.dataset.id));
      });
    },
    el: { bar: el.voiceBar, name: el.voiceName, state: el.voiceState, muteBtn: el.muteBtn, deafenBtn: el.deafenBtn,
          settingsBtn: el.voiceSettings, leaveBtn: el.voiceLeave, panel: el.voicePanel, inputSel: el.voiceInput,
          outputSel: el.voiceOutput, pttChk: el.voicePtt, volumes: el.voiceVolumes },
  });

  el.chatForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = el.chatText.value.trim();
    if (!text) return;
    el.chatText.value = "";
    chat.send(text).catch(function () {
      notice("That message did not send. Check the connection to the host.");
    });
  });

  // People waiting to be let in. The owner and the admins see them, and either one can answer.
  var requests = new Map();

  function renderRequests() {
    el.requestList.textContent = "";
    el.requestsBox.hidden = requests.size === 0;
    requests.forEach(function (request) {
      var row = document.createElement("li");
      var name = document.createElement("span");
      name.className = "name";
      name.textContent = request.name;
      name.title = request.address ? "From " + request.address : "";
      var allow = document.createElement("button");
      allow.className = "small";
      allow.textContent = "Let in";
      var deny = document.createElement("button");
      deny.className = "secondary small";
      deny.textContent = "Say no";
      allow.addEventListener("click", function () { decide(request.id, true); });
      deny.addEventListener("click", function () { decide(request.id, false); });
      row.append(name, allow, deny);
      el.requestList.append(row);
    });
  }

  function decide(id, allow) {
    post("/decide", { id: id, allow: allow }).catch(function () {
      notice("Someone else already answered that request.");
    });
    requests.delete(id);
    renderRequests();
  }

  el.myName.addEventListener("change", function () {
    var name = el.myName.value.trim();
    if (!name) return;
    savePref(PREFS.name, name);
    post("/name", { name: name }).catch(function () {});
  });

  // Leaving the server for good (the server menu, or the rail's): the server forgets this session, and the app takes
  // the server off the list.
  function leaveServer() {
    events.close();
    teardownAll();
    post("/leave", {}).catch(function () {}).finally(function () {
      notice("You left the server.");
      closeWindow();
    });
  }

  // --- picture in picture and fullscreen -------------------------------------------------------------------

  el.pip.addEventListener("click", function () {
    var tile = (focusKey && tiles.get(focusKey)) ||
      Array.from(tiles.values()).find(function (t) { return !t.classList.contains("stream-hidden"); });
    var video = tile && tile.querySelector("video");
    if (video && document.pictureInPictureEnabled) {
      video.requestPictureInPicture().catch(function () {
        notice("Picture in picture is not available for that picture.");
      });
    }
  });

  // --- the connection bar ----------------------------------------------------------------------------------

  function icon(name) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "ic");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.innerHTML = ICONS[name]; // fixed strings above, never page or network data
    return svg;
  }

  function metric(kind, iconName, text) {
    var item = document.createElement("span");
    item.className = "m " + kind;
    item.append(icon(iconName), document.createTextNode(text));
    return item;
  }

  function memberName(id) {
    return members.has(id) ? members.get(id).name : "a member";
  }

  // One chip per person: a dot for the state of the connection, the name, and only the numbers that apply, in colored
  // icons. The level says how much to show: 3 everything, 2 rates and frame rates, 1 rates, 0 the name alone.
  function buildChip(id, level) {
    var summary = summaries.get(id) || { state: "new", down: [], up: [] };
    var down = Stats.total(summary.down);
    var up = Stats.total(summary.up);
    var chip = document.createElement("span");
    chip.className = "chip";
    chip.dataset.id = id;
    chip.dataset.state = summary.state;
    if (down) {
      chip.dataset.downFps = String(Math.round(down.fps));
      chip.dataset.downRes = Stats.formatSize(down.w, down.h);
      chip.dataset.downKbps = String(Math.round(down.kbps));
    }
    if (up) {
      chip.dataset.upFps = String(Math.round(up.fps));
      chip.dataset.upRes = Stats.formatSize(up.w, up.h);
      chip.dataset.upKbps = String(Math.round(up.kbps));
    }
    var dot = document.createElement("span");
    dot.className = "dot";
    var name = document.createElement("span");
    name.className = "cname";
    name.textContent = memberName(id);
    chip.append(dot, name);
    if (level >= 1) {
      [["down", down], ["up", up]].forEach(function (pair) {
        var kind = pair[0];
        var total = pair[1];
        if (!total) return;
        chip.append(metric(kind, kind, Stats.formatRate(total.kbps)));
        if (level >= 2) chip.append(metric("fps", "fps", String(Math.round(total.fps))));
        if (level >= 3 && total.w > 0) chip.append(metric("size", "size", Stats.formatSize(total.w, total.h)));
      });
    }
    return chip;
  }

  function overflowing() {
    return el.statsbar.scrollWidth > el.statsbar.clientWidth + 1 || el.peerChips.scrollWidth > el.peerChips.clientWidth + 1;
  }

  function paint(ids, level, extra) {
    el.peerChips.textContent = "";
    ids.forEach(function (id) { el.peerChips.append(buildChip(id, level)); });
    if (extra) {
      var more = document.createElement("span");
      more.className = "chip more";
      more.textContent = "+" + extra;
      el.peerChips.append(more);
    }
  }

  // Show as much as fits. First less detail on every chip; if even the names alone are too many, the last few
  // become a "+N".
  function renderStatsBar(connected) {
    var ids = Array.from(peers.keys()).filter(function (id) {
      return members.has(id);
    });
    var level = Stats.highestFitting([3, 2, 1, 0], function (candidate) {
      paint(ids, candidate, 0);
      return !overflowing();
    });
    paint(ids, level, 0);
    var shown = ids.length;
    while (shown > 0 && overflowing()) {
      shown -= 1;
      paint(ids.slice(0, shown), 0, ids.length - shown);
    }
    el.statsbar.dataset.connected = String(connected);
    el.statsbar.dataset.peers = String(ids.length);
    el.statsbar.dataset.level = String(level);
    refreshTip();
  }

  function slotLabel(id, slot, outgoing) {
    if (outgoing) {
      var mineShare = shareInSlot(slot);
      return mineShare ? mineShare.label : "Screen " + (slot + 1);
    }
    var found = sharesOf(id).find(function (s) { return s.slot === slot; });
    return found ? found.label : "Screen " + (slot + 1);
  }

  var STATE_WORDS = {
    connected: "Connected", connecting: "Connecting", new: "Connecting", checking: "Connecting",
    disconnected: "Connection lost", failed: "Connection failed", closed: "Closed",
  };

  function tipLine(kind, iconName, text, extra) {
    var line = document.createElement("div");
    line.className = "line " + kind;
    line.append(icon(iconName));
    var span = document.createElement("span");
    span.textContent = text;
    line.append(span);
    if (extra) {
      var warn = document.createElement("span");
      warn.className = "warn";
      warn.textContent = extra;
      line.append(warn);
    }
    return line;
  }

  function slotText(s, busyWord) {
    var parts = [Stats.formatRate(s.kbps), Math.round(s.fps) + " fps"];
    if (s.w > 0) parts.push(Stats.formatSize(s.w, s.h));
    parts.push(busyWord + " " + Stats.formatCpu(s.cpu) + " CPU");
    return parts.join("  ·  ");
  }

  // The hover card for one connection: everything the chip leaves out.
  function buildTip(id) {
    var summary = summaries.get(id) || { state: "new", down: [], up: [], rtt: null, loss: 0 };
    el.tip.textContent = "";
    var title = document.createElement("h4");
    title.textContent = memberName(id);
    var state = document.createElement("span");
    state.className = "muted";
    state.textContent = STATE_WORDS[summary.state] || summary.state;
    title.append(state);
    el.tip.append(title);
    var down = Stats.flowing(summary.down);
    var up = Stats.flowing(summary.up);
    if (down.length) {
      var h = document.createElement("div");
      h.className = "sect";
      h.textContent = "Receiving from " + memberName(id);
      el.tip.append(h);
      down.forEach(function (s) {
        el.tip.append(tipLine("down", "down", slotLabel(id, s.slot, false) + ": " + slotText(s, "decoding")));
      });
    }
    if (up.length) {
      var u = document.createElement("div");
      u.className = "sect";
      u.textContent = "Sending to " + memberName(id);
      el.tip.append(u);
      up.forEach(function (s) {
        var limit = s.limit && s.limit !== "none" ? "limited by " + (s.limit === "cpu" ? "this PC's CPU" : s.limit) : "";
        el.tip.append(tipLine("up", "up", slotLabel(id, s.slot, true) + ": " + slotText(s, "encoding"), limit));
      });
    }
    if (!down.length && !up.length) {
      var none = document.createElement("div");
      none.className = "line";
      none.textContent = "No screens are being sent in either direction right now.";
      el.tip.append(none);
    }
    var net = document.createElement("div");
    net.className = "sect";
    net.textContent = "Network";
    el.tip.append(net);
    var line = document.createElement("div");
    line.className = "line";
    line.textContent = (summary.rtt == null ? "Round trip n/a" : "Round trip " + Math.round(summary.rtt) + " ms") +
      "  ·  Packet loss " + summary.loss.toFixed(1) + "%";
    el.tip.append(line);
    if (summary.way) {
      var wayLine = document.createElement("div");
      wayLine.className = "line";
      wayLine.textContent = Stats.wayText(summary.way);
      el.tip.append(wayLine);
    }
    var note = document.createElement("div");
    note.className = "note";
    note.textContent = "CPU figures are the share of the whole PC spent encoding or decoding, estimated from the codec's own timing.";
    el.tip.append(note);
  }

  function buildCpuTip() {
    el.tip.textContent = "";
    var title = document.createElement("h4");
    title.textContent = "This PC's CPU";
    el.tip.append(title);
    var line = document.createElement("div");
    line.className = "line";
    line.textContent = lastCpu === null
      ? "The reading is not available."
      : "PrivatePartyNet and its browser engine use " + lastCpu.toFixed(1) + "% of the whole processor right now.";
    el.tip.append(line);
    if (lastCpu !== null && lastCpuDetail && typeof lastCpuDetail.pc === "number") {
      var split = document.createElement("div");
      split.className = "line";
      split.textContent = "The app itself " + lastCpuDetail.own.toFixed(1) + "%, its browser engine " + lastCpuDetail.engine.toFixed(1) +
        "%. The whole PC is " + lastCpuDetail.pc.toFixed(1) + "% busy.";
      el.tip.append(split);
      var why = document.createElement("div");
      why.className = "note";
      why.textContent = "These are shares of processor time, summed over every logical processor. Task Manager's CPU column and its total " +
        "use their own method, so its numbers can differ.";
      el.tip.append(why);
    }
    if (upscaleInUse()) {
      var up = document.createElement("div");
      up.className = "line";
      up.textContent = el.upscaleNote.textContent;
      el.tip.append(up);
    }
  }

  function placeTip(anchor) {
    el.tip.hidden = false;
    var a = anchor.getBoundingClientRect();
    var t = el.tip.getBoundingClientRect();
    el.tip.style.left = Math.max(6, Math.min(a.left, window.innerWidth - t.width - 6)) + "px";
    el.tip.style.top = Math.max(6, a.top - t.height - 8) + "px";
  }

  function showTipFor(id, chip) {
    hoverId = id;
    buildTip(id);
    placeTip(chip);
  }

  function hideTip() {
    hoverId = null;
    el.tip.hidden = true;
  }

  // The chips are rebuilt every second, so the card is rebuilt with them while the pointer stays over a name.
  function refreshTip() {
    if (!hoverId || el.tip.hidden) return;
    var chip = el.peerChips.querySelector('.chip[data-id="' + hoverId + '"]');
    if (!chip) {
      hideTip();
      return;
    }
    buildTip(hoverId);
    placeTip(chip);
  }

  el.peerChips.addEventListener("mousemove", function (event) {
    var chip = event.target.closest(".chip[data-id]");
    if (!chip) {
      hideTip();
      return;
    }
    if (hoverId !== chip.dataset.id || el.tip.hidden) showTipFor(chip.dataset.id, chip);
  });
  el.peerChips.addEventListener("mouseleave", hideTip);
  el.cpuChip.addEventListener("mouseenter", function () {
    hoverId = null;
    buildCpuTip();
    placeTip(el.cpuChip);
  });
  el.cpuChip.addEventListener("mouseleave", hideTip);

  async function collect(peer) {
    var report = await peer.pc.getStats();
    var entries = [];
    report.forEach(function (s) { entries.push(s); });
    var mids = peer.pc.getTransceivers().map(function (t) { return t.mid; });
    return Stats.normalize(entries, mids, performance.now());
  }

  async function tickStats() {
    if (statsBusy) return;
    statsBusy = true;
    try {
      lastCpu = await readCpu();
      var cores = navigator.hardwareConcurrency || 1;
      var connected = 0;
      var activeOut = new Set(Array.from(shares.values(), function (s) { return s.slot; }));
      for (var entry of peers.entries()) {
        var id = entry[0];
        var peer = entry[1];
        var state = peer.pc.connectionState;
        if (state === "connected") connected += 1;
        var summary = { state: state, rtt: null, way: null, loss: 0, down: [], up: [] };
        try {
          var sample = await collect(peer);
          var previous = statSamples.get(id);
          statSamples.set(id, sample);
          var activeIn = new Set(sharesOf(id).map(function (s) { return s.slot; }));
          var result = Stats.diff(previous, sample, { cores: cores, activeIn: activeIn, activeOut: activeOut });
          summary.rtt = result.rtt;
          summary.way = result.way;
          summary.loss = result.loss;
          summary.down = result.down;
          summary.up = result.up;
        } catch (err) {
          // the connection is going away; the chip shows its state and no numbers
        }
        summaries.set(id, summary);
      }
      el.cpuValue.textContent = lastCpu === null ? "n/a" : lastCpu.toFixed(1) + "%";
      readUpscaleNote();
      adaptCaptureSizes();
      renderStatsBar(connected);
    } finally {
      statsBusy = false;
    }
  }

  setInterval(tickStats, 1000);

  // --- inside the one window ------------------------------------------------------------------------------
  // roombridge.js set window.PPNEmbedded when this room is a frame of the app's one window. The rail shows what this
  // room reports (unread messages, people waiting to join, the call), and the shell sends the person's look and theme,
  // whether this server is the one shown, and the call's controls when another server is shown.

  var shellShown = true;
  var lastReport = "";
  var companions = null; // who else is in this person's voice channel, so arrivals and departures can be heard

  // The one window plays the sounds (appshell.js), as Settings sets them; a room in a window of its own has none.
  function shellSound(name) {
    if (window.PPNEmbedded) window.PPNEmbedded.send({ ppn: "sound", name: name });
  }

  function checkCompanions() {
    var mine = voice.state.channel;
    var now = new Set();
    if (mine) members.forEach(function (m, id) { if (m.voice === mine && !(me && id === me.id)) now.add(id); });
    if (companions && mine && companions.channel === mine) {
      var arrived = Array.from(now).some(function (id) { return !companions.ids.has(id); });
      var went = Array.from(companions.ids).some(function (id) { return !now.has(id); });
      if (arrived) shellSound("peer-join");
      else if (went) shellSound("peer-leave");
    }
    companions = { channel: mine, ids: now };
  }

  function shellReport() {
    if (!window.PPNEmbedded) return;
    var inCall = voice.state.channel;
    var report = {
      ppn: "state",
      unread: chat.unreadTotal(),
      requests: requests.size,
      voice: inCall ? { channel: chat.channelName(inCall), muted: voice.state.muted, deafened: voice.state.deafened } : null,
    };
    var key = JSON.stringify(report);
    if (key === lastReport) return;
    lastReport = key;
    window.PPNEmbedded.send(report);
  }

  if (window.PPNEmbedded) {
    setInterval(shellReport, 700);
    window.addEventListener("ppn-shell", function (event) {
      var msg = event.detail || {};
      if (msg.ppn === "visible") {
        shellShown = Boolean(msg.on);
        if (shellShown && shownView.kind === "text") chat.seen();
        sendQualityToEveryone(); // screens are received only while this server is shown
        shellReport();
      } else if (msg.ppn === "appearance" && window.PPNAppearance && msg.look) {
        window.PPNAppearance.apply(document, msg.look);
      } else if (msg.ppn === "theme" && /^(light|dark|system)$/.test(String(msg.theme))) {
        THEME_OVERRIDE = msg.theme; // this person's theme, whatever the host picked for its own room
        document.documentElement.dataset.theme = msg.theme;
      } else if (msg.ppn === "voice") {
        var button = { mute: el.muteBtn, deafen: el.deafenBtn, leave: el.voiceLeave }[msg.action];
        if (button && voice.state.channel) button.click();
        shellReport();
      } else if (msg.ppn === "leave") {
        leaveServer();
      } else if (msg.ppn === "picture") {
        syncPicture();
      }
    });
  }

  // --- helpers -----------------------------------------------------------------------------------------------

  // This PC's CPU use by the app, read from the app. A join window reads it through its own feed.
  async function readCpu() {
    var url = FEED ? FEED + "/cpu?token=" + encodeURIComponent(FEED_TOKEN) : "/cpu";
    try {
      var response = await fetch(url, { cache: "no-store" });
      if (!response.ok) return null;
      lastCpuDetail = await response.json();
      return lastCpuDetail.app;
    } catch (err) {
      return null;
    }
  }

  // Upscaling's own cost, shown beside its choice: the time this page spent drawing upscaled frames over the last
  // second, as a share of the whole PC's CPU.
  function readUpscaleNote() {
    var share = (upscaleBusyMs / 1000 / (navigator.hardwareConcurrency || 1)) * 100;
    upscaleBusyMs = 0;
    el.upscaleNote.hidden = !upscaleInUse();
    if (!el.upscaleNote.hidden) el.upscaleNote.textContent = "Upscale: " + share.toFixed(1) + "% of this PC's CPU now";
  }

  function teardownAll() {
    voice.stopLocal();
    peers.forEach(function (entry, id) {
      closePeer(id);
    });
    shares.forEach(function (share) {
      share.stop();
      share.track.stop();
    });
    shares.clear();
    updateGoLive();
    renderMyShares();
    renderTiles();
  }

  // Leaving the server takes its room off the rail in the app. In a plain browser there is nothing to close and the page
  // stays, showing that the member left.
  function closeWindow() {
    var api = window.pywebview && window.pywebview.api;
    if (api && api.close_window) api.close_window().catch(function () {});
  }

  // Page problems go to the app log, so a share that fails in the app's window can be traced.
  function report(message) {
    post("/log", { message: message }).catch(function () {});
  }

  window.addEventListener("error", function (event) {
    report("page error: " + event.message + " (" + event.filename + ":" + event.lineno + ")");
  });
  window.addEventListener("unhandledrejection", function (event) {
    report("page promise rejected: " + (event.reason && event.reason.message ? event.reason.message : event.reason));
  });

  function notice(text) {
    el.status.textContent = text;
    el.status.hidden = false;
    setTimeout(function () { el.status.hidden = true; }, 6000);
  }

  function send(to, body) {
    return post("/signal", Object.assign({ to: to }, body));
  }

  async function post(path, body) {
    var response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      var reason = await response.text().catch(function () { return ""; });
      throw new Error(reason || path + " returned " + response.status); // the server's own words when it gives them
    }
  }

  // A member's room hands the chat it receives to the app, which keeps an encrypted copy on this PC. The host's own room
  // (usesFrames) does not: the server stores everything on the host already.
  function keepChat() {
    if (usesFrames()) return null;
    return function (serverId, serverName, channels, messages) {
      var bridge = window.pywebview && window.pywebview.api;
      if (!bridge || !bridge.keep_chat) return Promise.resolve(null);
      return bridge.keep_chat(serverId, serverName, channels, messages);
    };
  }

  function keepPicture(hash) {
    var bridge = window.pywebview && window.pywebview.api;
    if (!bridge || !bridge.keep_blob) return;
    fetch("/blob?hash=" + encodeURIComponent(hash))
      .then(function (response) { return response.ok ? response.blob() : null; })
      .then(function (blob) {
        if (!blob) return null;
        return new Promise(function (resolve) {
          var reader = new FileReader();
          reader.onload = function () { resolve(String(reader.result).split(",")[1]); };
          reader.readAsDataURL(blob);
        });
      })
      .then(function (data) { if (data) bridge.keep_blob(hash, data); })
      .catch(function () {});
  }

  async function uploadBlob(blob) {
    var response = await fetch("/blob", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: blob,
    });
    if (!response.ok) throw new Error(response.status === 403 ? "Pictures can't be sent right now." : (await response.text()) || "That picture did not upload.");
    return response.json();
  }

  // A file of any kind, sent as it is with its progress showing; the server posts it once it has all arrived.
  // Returns { promise, abort }.
  function uploadFile(file, channel, progress) {
    var request = new XMLHttpRequest();
    var promise = new Promise(function (resolve, reject) {
      var query = "channel=" + encodeURIComponent(channel) + "&name=" + encodeURIComponent(file.name) +
        "&type=" + encodeURIComponent(file.type || "");
      request.open("POST", "/file?" + query);
      request.setRequestHeader("Content-Type", "application/octet-stream");
      request.upload.onprogress = function (event) { if (progress) progress(event.loaded, event.total || file.size); };
      request.onload = function () {
        if (request.status === 200) resolve();
        else reject(new Error(request.responseText || "The host refused it (" + request.status + ")."));
      };
      request.onerror = function () { reject(new Error("The connection to the host broke off.")); };
      request.onabort = function () { var e = new Error("stopped"); e.name = "AbortError"; reject(e); };
      request.send(file);
    });
    return { promise: promise, abort: function () { request.abort(); } };
  }

  // --- the storage meter, and the file limits the owner and admins set ---------------------------------------

  var storageEl = {
    meter: document.getElementById("storageMeter"), text: document.getElementById("storageText"),
    bar: document.getElementById("storageBar"), edit: document.getElementById("storageEdit"),
    panel: document.getElementById("storagePanel"), now: document.getElementById("storageNow"),
    limitKind: document.getElementById("limitKind"), limitSize: document.getElementById("limitSize"),
    limitUnit: document.getElementById("limitUnit"), capKind: document.getElementById("capKind"),
    capSize: document.getElementById("capSize"), capUnit: document.getElementById("capUnit"),
    problem: document.getElementById("storageProblem"), save: document.getElementById("storageSave"),
    close: document.getElementById("storageClose"),
  };
  var UNIT = { MB: 1024 * 1024, GB: 1024 * 1024 * 1024 };

  function setStorage(info) {
    if (!info || !storageEl.meter) return;
    chat.setStorage(info);
    var text = PartyChat.storageText(info);
    storageEl.meter.hidden = !text;
    storageEl.text.textContent = text;
    storageEl.text.title = text;
    var share = info.cap ? Math.min(1, info.used / info.cap) : 0;
    storageEl.bar.style.width = Math.round(share * 100) + "%";
    storageEl.meter.classList.toggle("open-ended", !info.cap);
    storageEl.meter.classList.toggle("full", share >= 0.9);
    storageEl.edit.hidden = !(me && (me.role === "host" || me.role === "admin"));
  }

  function showLimit(bytes, kind, size, unit) {
    kind.value = bytes ? "size" : "none";
    if (bytes) {
      var gb = bytes % UNIT.GB === 0;
      unit.value = gb ? "GB" : "MB";
      size.value = String(bytes / UNIT[unit.value]);
    }
    size.disabled = unit.disabled = kind.value === "none";
  }

  function readLimit(kind, size, unit) {
    if (kind.value === "none") return null;
    var n = Number(size.value);
    return isFinite(n) && n > 0 ? Math.round(n * UNIT[unit.value]) : NaN;
  }

  function openStoragePanel() {
    var info = chat.storage();
    if (!info) return;
    showLimit(info.limit, storageEl.limitKind, storageEl.limitSize, storageEl.limitUnit);
    showLimit(info.cap, storageEl.capKind, storageEl.capSize, storageEl.capUnit);
    storageEl.now.textContent = PartyChat.storageText(info) + ".";
    storageEl.problem.hidden = true;
    var r = storageEl.meter.getBoundingClientRect();
    storageEl.panel.style.left = Math.max(8, r.left + 8) + "px";
    storageEl.panel.style.top = (r.bottom + 6) + "px";
    storageEl.panel.hidden = false;
  }

  if (storageEl.meter) {
    storageEl.edit.addEventListener("click", function (event) {
      event.stopPropagation();
      if (storageEl.panel.hidden) openStoragePanel(); else storageEl.panel.hidden = true;
    });
    [[storageEl.limitKind, storageEl.limitSize, storageEl.limitUnit], [storageEl.capKind, storageEl.capSize, storageEl.capUnit]]
      .forEach(function (row) {
        row[0].addEventListener("change", function () { row[1].disabled = row[2].disabled = row[0].value === "none"; });
      });
    storageEl.close.addEventListener("click", function () { storageEl.panel.hidden = true; });
    storageEl.save.addEventListener("click", function () {
      var limit = readLimit(storageEl.limitKind, storageEl.limitSize, storageEl.limitUnit);
      var cap = readLimit(storageEl.capKind, storageEl.capSize, storageEl.capUnit);
      var problem = "";
      if (Number.isNaN(limit) || Number.isNaN(cap)) problem = "Each size is a number above 0.";
      else if ((limit !== null && limit < UNIT.MB) || (cap !== null && cap < UNIT.MB)) problem = "Each size is at least 1 MB.";
      else if (limit !== null && cap !== null && cap < 10 * limit) {
        problem = "The cap has to be at least ten times the largest file (" + PartyChat.humanSize(10 * limit) + " or more).";
      }
      storageEl.problem.textContent = problem;
      storageEl.problem.hidden = !problem;
      if (problem) return;
      post("/storage", { limit: limit, cap: cap }).then(function () {
        storageEl.panel.hidden = true;
        notice("File limits saved.");
      }).catch(function (err) {
        storageEl.problem.textContent = (err && err.message) || "Those limits were not saved.";
        storageEl.problem.hidden = false;
      });
    });
    document.addEventListener("click", function (event) {
      if (!storageEl.panel.hidden && !storageEl.panel.contains(event.target) && event.target !== storageEl.edit) storageEl.panel.hidden = true;
    });
  }

  // --- the server's name and icon, the server menu, and this person's picture ---------------------------------------

  var serverName = "Server";
  var serverIcon = null;
  var MAX_PICTURE = 2 * 1024 * 1024;

  function isHash(value) {
    return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  }

  function markNode(hash) {
    if (hash) {
      var img = document.createElement("img");
      img.alt = "";
      img.src = "/blob?hash=" + hash;
      return img;
    }
    return window.PPNIcons ? window.PPNIcons.node("hat") : document.createTextNode("");
  }

  function setServerLook(name, icon) {
    if (typeof name === "string" && name) serverName = name;
    serverIcon = isHash(icon) ? icon : null;
    el.partyName.textContent = serverName;
    document.title = serverName + " - PrivatePartyNet";
    el.serverMark.replaceChildren(markNode(serverIcon));
    el.serverMark.classList.toggle("has-picture", Boolean(serverIcon));
    reportLook();
  }

  function asBase64(blob) {
    return new Promise(function (resolve) {
      var reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result).split(",")[1] || ""); };
      reader.onerror = function () { resolve(""); };
      reader.readAsDataURL(blob);
    });
  }

  // Tell the app this server's name and icon, for its place on the rail (kept for when the server is out of reach).
  function reportLook() {
    var api = window.pywebview && window.pywebview.api;
    if (!api || !api.server_look || !me) return;
    var icon = serverIcon;
    (icon ? fetch("/blob?hash=" + icon).then(function (r) { return r.ok ? r.blob() : null; })
          .then(function (blob) { return blob ? asBase64(blob) : ""; }) : Promise.resolve(""))
      .then(function (data) { if (icon === serverIcon) return api.server_look(serverName, data); })
      .catch(function () { /* the next change tries again */ });
  }

  // Hand this server the person's own picture from Settings, when it does not have that one yet.
  var pictureSent = undefined;
  function syncPicture() {
    var api = window.pywebview && window.pywebview.api;
    if (!api || !api.my_picture || !me) return;
    api.my_picture().then(function (picture) {
      var want = picture && isHash(picture.hash) ? picture.hash : null;
      if (want === (me.avatar || null) || want === pictureSent) return null;
      pictureSent = want;
      var body = new Uint8Array(0);
      if (want) {
        var raw = atob(picture.data || "");
        body = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i += 1) body[i] = raw.charCodeAt(i);
      }
      return fetch("/avatar", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: body });
    }).catch(function () { pictureSent = undefined; });
  }
  window.addEventListener("pywebviewready", function () { syncPicture(); reportLook(); });

  function isOwnerOrAdmin() {
    return Boolean(me && (me.role === "host" || me.role === "admin"));
  }

  el.serverMenuBtn.addEventListener("click", function (event) {
    event.stopPropagation();
    var box = el.serverMenuBtn.getBoundingClientRect();
    var entries = [];
    if (isOwnerOrAdmin()) {
      entries.push({ label: "Server settings", run: openServerPanel });
      var info = chat.storage();
      if (info && info.files !== false) entries.push({ label: "File limits", run: openStoragePanel });
    }
    if (me && me.role !== "host") {
      if (entries.length) entries.push("-");
      entries.push({ label: "Leave server", danger: true, run: function () {
        // In the app the rail asks first and takes the server off the list; in a plain browser the room leaves at once.
        if (window.PPNEmbedded) window.PPNEmbedded.send({ ppn: "leave-server" });
        else leaveServer();
      } });
    }
    if (!entries.length) entries.push({ label: "Only the owner and admins change this server", run: function () {} });
    showMenu(entries, box.left + 8, box.bottom + 4);
  });

  var chosenIcon = null; // a File picked in Server settings, or "" to take the icon away, or null for no change

  function openServerPanel() {
    chosenIcon = null;
    el.serverNameInput.value = serverName;
    el.serverIconPreview.replaceChildren(markNode(serverIcon));
    el.serverProblem.hidden = true;
    var r = el.serverMenuBtn.getBoundingClientRect();
    el.serverPanel.style.left = Math.max(8, r.left + 8) + "px";
    el.serverPanel.style.top = (r.bottom + 6) + "px";
    el.serverPanel.hidden = false;
    el.serverNameInput.focus();
  }

  function serverProblem(text) {
    el.serverProblem.textContent = text;
    el.serverProblem.hidden = !text;
  }

  el.serverIconFile.addEventListener("change", function () {
    var file = el.serverIconFile.files && el.serverIconFile.files[0];
    el.serverIconFile.value = "";
    if (!file) return;
    if (file.size > MAX_PICTURE) { serverProblem("An icon can be up to 2 MB."); return; }
    if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) { serverProblem("Use a PNG, JPEG, GIF or WebP picture."); return; }
    serverProblem("");
    chosenIcon = file;
    var img = document.createElement("img");
    img.alt = "";
    img.src = URL.createObjectURL(file);
    el.serverIconPreview.replaceChildren(img);
  });
  el.serverIconClear.addEventListener("click", function () {
    chosenIcon = "";
    el.serverIconPreview.replaceChildren(markNode(null));
  });
  el.serverClose.addEventListener("click", function () { el.serverPanel.hidden = true; });
  el.serverSave.addEventListener("click", function () {
    var name = el.serverNameInput.value.trim();
    if (!name) { serverProblem("Give the server a name."); return; }
    var steps = [];
    if (name !== serverName) steps.push(post("/server", { name: name }));
    if (chosenIcon !== null) {
      steps.push(fetch("/server/icon", { method: "POST", headers: { "Content-Type": "application/octet-stream" },
                                        body: chosenIcon || new Uint8Array(0) })
        .then(function (response) {
          if (!response.ok) return response.text().then(function (t) { throw new Error(t || "The icon was not saved."); });
        }));
    }
    Promise.all(steps).then(function () {
      el.serverPanel.hidden = true;
      if (steps.length) notice("Server settings saved.");
    }).catch(function (err) { serverProblem((err && err.message) || "That was not saved."); });
  });
  document.addEventListener("click", function (event) {
    if (!el.serverPanel.hidden && !el.serverPanel.contains(event.target) && !el.serverMenuBtn.contains(event.target) &&
        !el.ctxMenu.contains(event.target)) el.serverPanel.hidden = true;
  });

  async function getJson(path) {
    var response = await fetch(path);
    if (!response.ok) throw new Error(path + " returned " + response.status);
    return response.json();
  }

  function restorePrefs() {
    el.receive.value = readPref(PREFS.receive) || "fit";
    el.upscale.value = readPref(PREFS.upscale) || "off";
    el.ceiling.value = readPref(PREFS.ceiling) || "source";
    el.fps.value = readPref(PREFS.fps) || "30";
    el.pointer.value = readPref(PREFS.pointer) || "on";
    myCeiling = ceilingFromChoices();
    setSideHidden(readPref(PREFS.side) === "hidden");
  }

  function readPref(key) {
    try {
      return localStorage.getItem(key);
    } catch (err) {
      return null; // storage can be blocked; the defaults still work
    }
  }

  function savePref(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (err) {
      // not saved; the choice still applies for this visit
    }
  }

  updateGoLive();
  updateCount();
})();
