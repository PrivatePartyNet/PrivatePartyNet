// Channels and chat for one server: the channel list, each channel's messages, scroll-back, unread counts and the
// owner's and admins' channel controls. The page (room.js) feeds it the server's events and gives it the few things it
// needs from outside: post, getJson, notice, avatar, and what to do when someone joins or leaves a voice channel.
// All text from other people goes in through textContent, never innerHTML.
(function () {
  "use strict";

  var PAGE_PRELOAD = 200; // the server sends this many when a member connects; fewer back means there is no more
  var MAX_SIDE = 1600; // a picture is shrunk so its longest side is at most this many pixels
  var MAX_BYTES = 2 * 1024 * 1024; // the most the server takes; keep in step with MAX_IMAGE in server.py
  var PICTURE_TYPES = /^image\/(png|jpeg|gif|webp)$/;

  // The size a picture is shrunk to: its own shape, longest side at most `max`. Never enlarges.
  function fitSize(width, height, max) {
    if (width <= max && height <= max) return { w: width, h: height };
    var ratio = max / Math.max(width, height);
    return { w: Math.max(1, Math.round(width * ratio)), h: Math.max(1, Math.round(height * ratio)) };
  }

  function canvasBlob(canvas, type, quality) {
    return new Promise(function (resolve) { canvas.toBlob(resolve, type, quality); });
  }

  // Shrinks and re-encodes a picture in the page. Drawing it onto a canvas and saving it again also drops what the
  // file carried besides the picture (the camera's position and time among it). A GIF is sent as it is, to stay animated.
  async function preparePicture(file) {
    if (!PICTURE_TYPES.test(file.type)) throw new Error("Only PNG, JPEG, GIF and WebP pictures can be sent.");
    var bitmap = await createImageBitmap(file);
    if (file.type === "image/gif") {
      if (file.size > MAX_BYTES) throw new Error("That GIF is over 2 MB.");
      return { blob: file, w: bitmap.width, h: bitmap.height };
    }
    var size = fitSize(bitmap.width, bitmap.height, MAX_SIDE);
    var canvas = document.createElement("canvas");
    canvas.width = size.w;
    canvas.height = size.h;
    canvas.getContext("2d").drawImage(bitmap, 0, 0, size.w, size.h);
    var quality = 0.82;
    var blob = await canvasBlob(canvas, "image/webp", quality);
    if (!blob || blob.type !== "image/webp") blob = await canvasBlob(canvas, "image/jpeg", quality);
    while (blob && blob.size > MAX_BYTES && quality > 0.4) {
      quality -= 0.15;
      blob = await canvasBlob(canvas, blob.type, quality);
    }
    if (!blob || blob.size > MAX_BYTES) throw new Error("That picture is still over 2 MB after shrinking it.");
    return { blob: blob, w: size.w, h: size.h };
  }

  function create(options) {
    var el = options.el;
    var state = {
      channels: [],
      current: null,
      voice: null,
      role: "member",
      meId: null,
      members: new Map(),
      buffers: new Map(),
    };
    var lastSpeaker = null;
    var editing = null; // { id, kind, mode } while a channel name is being typed
    var server = null; // { id, name } from the hello
    var toKeep = []; // messages not yet handed to this PC's own store
    var keepTimer = null;

    // Hand what arrives to this PC's own store, in batches, through options.keep (the app's bridge). The host's own
    // window has no keep: the party there stores everything itself.
    function keep(list) {
      if (!options.keep || !server) return;
      Array.prototype.push.apply(toKeep, list);
      if (!keepTimer) keepTimer = setTimeout(flushKeep, 800);
    }

    function flushKeep() {
      keepTimer = null;
      var batch = toKeep.splice(0, 500);
      if (!batch.length) return;
      options.keep(server.id, server.name, state.channels, batch).then(function (result) {
        (result && result.missing ? result.missing : []).forEach(function (hash) {
          if (options.keepBlob) options.keepBlob(hash);
        });
      }).catch(function () {});
      if (toKeep.length) keepTimer = setTimeout(flushKeep, 800);
    }

    function buffer(id) {
      if (!state.buffers.has(id)) state.buffers.set(id, { list: [], done: false, loading: false, unread: 0 });
      return state.buffers.get(id);
    }

    function isStaff() {
      return state.role === "host" || state.role === "admin";
    }

    function textChannels() {
      return state.channels.filter(function (c) { return c.kind === "text"; });
    }

    function channelById(id) {
      return state.channels.find(function (c) { return c.id === id; }) || null;
    }

    // --- events from the server ------------------------------------------------------------------------------

    function hello(msg) {
      server = msg.server ? { id: msg.server, name: msg.party } : null;
      state.meId = msg.you.id;
      state.role = msg.you.role;
      setChannels(msg.channels || []);
      renderComposer();
    }

    function setChannels(list) {
      state.channels = list;
      if (state.current === null || !channelById(state.current) || channelById(state.current).kind !== "text") {
        var first = textChannels()[0];
        state.current = first ? first.id : null;
      }
      state.buffers.forEach(function (_, id) {
        if (!channelById(id)) state.buffers.delete(id);
      });
      if (state.voice && !channelById(state.voice)) state.voice = null;
      renderChannels();
      renderMessages();
    }

    function history(msg) {
      var buf = buffer(msg.channel);
      buf.list = msg.messages.slice();
      buf.done = msg.messages.length < PAGE_PRELOAD;
      keep(msg.messages);
      if (msg.channel === state.current) renderMessages();
    }

    function message(msg) {
      var buf = buffer(msg.channel);
      if (buf.list.some(function (m) { return m.id === msg.id; })) return;
      buf.list.push(msg);
      keep([msg]);
      if (msg.channel === state.current) {
        appendMessage(msg);
        if (document.hidden) buf.unread += 1;
      } else {
        buf.unread += 1;
        renderChannels();
      }
    }

    function setRole(role) {
      state.role = role;
      renderChannels();
      renderComposer();
    }

    function setMembers(members) {
      state.members = members;
      var mine = members.get(state.meId);
      if (mine && state.voice !== mine.voice) state.voice = mine.voice || null;
      renderChannels();
    }

    // --- choosing a channel --------------------------------------------------------------------------------

    function select(id) {
      var channel = channelById(id);
      if (!channel || channel.kind !== "text") return;
      state.current = id;
      buffer(id).unread = 0;
      renderChannels();
      renderMessages();
      el.chatText.focus();
    }

    // --- messages ------------------------------------------------------------------------------------------

    function senderKey(message) {
      return message.user || message.from;
    }

    function messageNode(message, continued) {
      var line = document.createElement("div");
      line.className = "message";
      if (continued) line.classList.add("cont");
      var body = document.createElement("div");
      body.className = "body";
      var head = document.createElement("div");
      head.className = "head";
      var who = document.createElement("b");
      who.textContent = message.name;
      var when = document.createElement("span");
      when.className = "time";
      when.textContent = message.at;
      head.append(who, when);
      var text = document.createElement("div");
      text.className = "text";
      text.textContent = message.text;
      body.append(head);
      if (message.text) body.append(text);
      if (message.image) body.append(pictureNode(message));
      line.append(options.avatar(message.name, senderKey(message)), body);
      return line;
    }

    function pictureNode(message) {
      var image = document.createElement("img");
      image.className = "chat-image";
      image.loading = "lazy";
      image.alt = "Picture from " + message.name;
      image.src = "/blob?hash=" + encodeURIComponent(message.image.hash);
      if (message.image.w && message.image.h) image.style.aspectRatio = message.image.w + " / " + message.image.h;
      image.addEventListener("click", function () { showLightbox(image.src); });
      return image;
    }

    function showLightbox(src) {
      if (!el.lightbox) return;
      el.lightbox.querySelector("img").src = src;
      el.lightbox.hidden = false;
    }

    if (el.lightbox) {
      el.lightbox.addEventListener("click", function () { el.lightbox.hidden = true; });
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !el.lightbox.hidden) el.lightbox.hidden = true;
      });
    }

    // Sending a picture: shrink it here, upload it, then post a message that names it.
    function sendPicture(file) {
      if (state.role === "guest" || !state.current) return Promise.resolve();
      var caption = el.chatText.value.trim();
      return preparePicture(file)
        .then(function (prepared) {
          return options.upload(prepared.blob).then(function (info) {
            el.chatText.value = "";
            return options.post("/chat", {
              text: caption, channel: state.current, image: { hash: info.hash, w: prepared.w, h: prepared.h },
            });
          });
        })
        .catch(function (err) { options.notice(err && err.message ? err.message : "That picture did not send."); });
    }

    function pictureFrom(files) {
      for (var i = 0; i < files.length; i++) {
        if (PICTURE_TYPES.test(files[i].type)) return files[i];
      }
      return null;
    }

    if (el.attachBtn && el.attachFile) {
      el.attachBtn.addEventListener("click", function () { el.attachFile.click(); });
      el.attachFile.addEventListener("change", function () {
        var file = pictureFrom(el.attachFile.files);
        el.attachFile.value = "";
        if (file) sendPicture(file);
      });
    }
    el.chatText.addEventListener("paste", function (event) {
      var file = pictureFrom((event.clipboardData && event.clipboardData.files) || []);
      if (!file) return;
      event.preventDefault();
      sendPicture(file);
    });
    if (el.dropZone) {
      el.dropZone.addEventListener("dragover", function (event) {
        if (event.dataTransfer && Array.prototype.indexOf.call(event.dataTransfer.types, "Files") >= 0) event.preventDefault();
      });
      el.dropZone.addEventListener("drop", function (event) {
        var file = pictureFrom((event.dataTransfer && event.dataTransfer.files) || []);
        if (!file) return;
        event.preventDefault();
        sendPicture(file);
      });
    }

    function renderMessages() {
      el.messages.textContent = "";
      lastSpeaker = null;
      var channel = channelById(state.current);
      el.chatTitle.textContent = channel ? channel.name : "Chat";
      if (!channel) return;
      var buf = buffer(channel.id);
      if (buf.done && buf.list.length) {
        var top = document.createElement("p");
        top.className = "muted start";
        top.textContent = "This is the start of #" + channel.name + ".";
        el.messages.append(top);
      }
      buf.list.forEach(function (m) {
        el.messages.append(messageNode(m, lastSpeaker !== null && lastSpeaker === senderKey(m)));
        lastSpeaker = senderKey(m);
      });
      el.messages.scrollTop = el.messages.scrollHeight;
    }

    function appendMessage(message) {
      var stick = el.messages.scrollHeight - el.messages.scrollTop - el.messages.clientHeight < 80;
      el.messages.append(messageNode(message, lastSpeaker !== null && lastSpeaker === senderKey(message)));
      lastSpeaker = senderKey(message);
      if (stick || message.from === state.meId) el.messages.scrollTop = el.messages.scrollHeight;
    }

    // Older messages load when the list is scrolled to its top.
    function loadOlder() {
      var channel = channelById(state.current);
      if (!channel) return;
      var buf = buffer(channel.id);
      if (buf.done || buf.loading || !buf.list.length) return;
      buf.loading = true;
      var before = buf.list[0].ts;
      options.getJson("/history?channel=" + encodeURIComponent(channel.id) + "&before=" + before + "&limit=50")
        .then(function (data) {
          if (channel.id !== state.current || !data.messages) return;
          if (data.messages.length < 50) buf.done = true;
          var known = new Set(buf.list.map(function (m) { return m.id; }));
          var fresh = data.messages.filter(function (m) { return !known.has(m.id); });
          keep(fresh);
          var height = el.messages.scrollHeight;
          buf.list = fresh.concat(buf.list);
          renderMessages();
          el.messages.scrollTop = el.messages.scrollHeight - height;
        })
        .catch(function () {})
        .finally(function () { buf.loading = false; });
    }

    el.messages.addEventListener("scroll", function () {
      if (el.messages.scrollTop < 30) loadOlder();
    });

    function send(text) {
      if (!state.current || !text) return Promise.resolve();
      return options.post("/chat", { text: text, channel: state.current });
    }

    function renderComposer() {
      var guest = state.role === "guest";
      el.chatText.disabled = guest;
      el.chatForm.querySelector("button[type=submit]").disabled = guest;
      if (el.attachBtn) el.attachBtn.disabled = guest;
      el.chatText.placeholder = guest ? "Guests can read the chat but not post" : "Message this channel";
    }

    // --- the channel list ----------------------------------------------------------------------------------

    function occupants(channelId) {
      var people = [];
      state.members.forEach(function (m) {
        if (m.voice === channelId && m.role !== "viewer") people.push(m);
      });
      return people;
    }

    function smallButton(label, title, handler) {
      var button = document.createElement("button");
      button.className = "secondary small";
      button.textContent = label;
      button.title = title;
      button.addEventListener("click", function (event) {
        event.stopPropagation();
        handler();
      });
      return button;
    }

    function nameEditor(initial, done) {
      var form = document.createElement("form");
      form.className = "channel-edit";
      var input = document.createElement("input");
      input.maxLength = 32;
      input.value = initial;
      input.setAttribute("aria-label", "Channel name");
      var ok = document.createElement("button");
      ok.type = "submit";
      ok.className = "small";
      ok.textContent = "Save";
      var cancel = smallButton("Cancel", "Cancel", function () { editing = null; renderChannels(); });
      form.append(input, ok, cancel);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var name = input.value.trim();
        editing = null;
        if (name) done(name);
        renderChannels();
      });
      setTimeout(function () { input.focus(); input.select(); }, 0);
      return form;
    }

    function channelAction(body) {
      return options.post("/channel", body).catch(function () {
        options.notice("That channel change was refused.");
      });
    }

    function renderChannels() {
      el.channelList.textContent = "";
      el.addText.hidden = el.addVoice.hidden = !isStaff();
      state.channels.forEach(function (channel) {
        var item = document.createElement("li");
        var isVoice = channel.kind === "voice";
        item.className = "channel " + channel.kind + (channel.id === state.current ? " current" : "");
        if (editing && editing.id === channel.id) {
          item.append(nameEditor(channel.name, function (name) {
            channelAction({ action: "rename", id: channel.id, name: name });
          }));
          el.channelList.append(item);
          return;
        }
        var row = document.createElement("div");
        row.className = "channel-row";
        var label = document.createElement("span");
        label.className = "channel-name";
        label.textContent = (isVoice ? "Voice  " : "#  ") + channel.name;
        row.append(label);
        var unread = buffer(channel.id).unread;
        if (!isVoice && unread && channel.id !== state.current) {
          var badge = document.createElement("span");
          badge.className = "unread";
          badge.textContent = unread > 99 ? "99+" : String(unread);
          row.append(badge);
        }
        if (isVoice && state.role !== "guest") {
          var inHere = state.voice === channel.id;
          row.append(smallButton(inHere ? "Leave" : "Join", inHere ? "Leave this voice channel" : "Join this voice channel",
            function () { options.onVoice(inHere ? null : channel.id, channel.name); }));
        }
        if (isStaff()) {
          row.append(smallButton("Rename", "Rename this channel", function () {
            editing = { id: channel.id };
            renderChannels();
          }));
          row.append(smallButton("Delete", "Delete this channel (its saved messages stay in the store)", function () {
            channelAction({ action: "remove", id: channel.id });
          }));
        }
        item.append(row);
        if (!isVoice) {
          row.addEventListener("click", function () { select(channel.id); });
        } else {
          var people = occupants(channel.id);
          if (people.length) {
            var list = document.createElement("ul");
            list.className = "voice-people";
            people.forEach(function (person) {
              var li = document.createElement("li");
              li.textContent = person.name + (person.id === state.meId ? " (you)" : "");
              list.append(li);
            });
            item.append(list);
          }
        }
        el.channelList.append(item);
      });
      if (editing && editing.id === null) {
        var adding = document.createElement("li");
        adding.className = "channel";
        adding.append(nameEditor("", function (name) {
          channelAction({ action: "add", name: name, kind: editing_kind });
        }));
        el.channelList.append(adding);
      }
    }

    var editing_kind = "text";
    function startAdd(kind) {
      editing_kind = kind;
      editing = { id: null };
      renderChannels();
    }
    el.addText.addEventListener("click", function () { startAdd("text"); });
    el.addVoice.addEventListener("click", function () { startAdd("voice"); });

    return {
      hello: hello,
      setChannels: setChannels,
      history: history,
      message: message,
      setRole: setRole,
      setMembers: setMembers,
      send: send,
      select: select,
      current: function () { return state.current; },
      voiceChannel: function () { return state.voice; },
    };
  }

  var api = { create: create, fitSize: fitSize, preparePicture: preparePicture, MAX_SIDE: MAX_SIDE, MAX_BYTES: MAX_BYTES };
  if (typeof window !== "undefined") window.PartyChat = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})();
