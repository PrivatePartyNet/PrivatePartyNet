// Channels and chat for one server: the channel list, each channel's messages, scroll-back, unread counts and the
// owner's and admins' channel controls, and the channels this person muted. The page (room.js) feeds it the server's events and gives it the few things it
// needs from outside: post, getJson, notice, avatar, and what to do when someone joins or leaves a voice channel.
// All text from other people goes in through textContent, never innerHTML.
(function () {
  "use strict";

  var PAGE_PRELOAD = 200; // the server sends this many when a member connects; fewer back means there is no more
  var MAX_BYTES = 12 * 1024 * 1024; // the most the server takes; keep in step with MAX_IMAGE in server.py
  var PICTURE_TYPES = /^image\/(png|jpeg|gif|webp)$/;
  var QUALITIES = [0.92, 0.85, 0.78, 0.7, 0.6];

  // The ways a picture can be saved, best first. The pixels and the size never change; only how they are stored does.
  // A PNG stays a lossless PNG when it fits. When it does not, and for JPEG and WebP, the same pixels are saved
  // lossy at a high quality, then lower until the file fits.
  function encodingPlan(type) {
    var plan = [];
    if (type === "image/png") plan.push(["image/png", undefined]);
    var lossy = type === "image/jpeg" ? "image/jpeg" : "image/webp";
    QUALITIES.forEach(function (q) { plan.push([lossy, q]); });
    if (type === "image/png") plan.push(["image/jpeg", 0.85]); // last resort if the page cannot save WebP
    return plan;
  }

  function canvasBlob(canvas, type, quality) {
    return new Promise(function (resolve) { canvas.toBlob(resolve, type, quality); });
  }

  // Saves a picture again in the page at its own size. Drawing it onto a canvas and saving it also drops what the
  // file carried besides the picture (the camera's position and time among it) and turns it the way it is meant to be
  // seen. A GIF is sent as it is, to stay animated.
  async function preparePicture(file) {
    if (!PICTURE_TYPES.test(file.type)) throw new Error("Only PNG, JPEG, GIF and WebP pictures can be sent.");
    var bitmap = await createImageBitmap(file);
    if (file.type === "image/gif") {
      if (file.size > MAX_BYTES) throw new Error("That GIF is over 12 MB.");
      return { blob: file, w: bitmap.width, h: bitmap.height };
    }
    var canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext("2d").drawImage(bitmap, 0, 0);
    var plan = encodingPlan(file.type);
    for (var i = 0; i < plan.length; i++) {
      var blob = await canvasBlob(canvas, plan[i][0], plan[i][1]);
      if (blob && blob.type === plan[i][0] && blob.size <= MAX_BYTES) return { blob: blob, w: bitmap.width, h: bitmap.height };
    }
    throw new Error("That picture does not fit in 12 MB at its full size. Its size is never reduced, so crop it or send a smaller file.");
  }

  var MAX_DROPPED = 10; // files sent from one drop
  var MIN_FREE = 512 * 1024 * 1024; // the host's disk keeps this much free (server.py MIN_FREE_BYTES)

  // 3.4 GB, 512 KB, 12 bytes
  function humanSize(n) {
    var units = [["TB", Math.pow(1024, 4)], ["GB", Math.pow(1024, 3)], ["MB", 1024 * 1024], ["KB", 1024]];
    for (var i = 0; i < units.length; i++) {
      if (n >= units[i][1]) return (Math.round(n / units[i][1] * 10) / 10) + " " + units[i][0];
    }
    return n + (n === 1 ? " byte" : " bytes");
  }

  // What a file's card calls it and which icon it gets, from its name and type.
  function fileKind(name, type) {
    var ext = String(name || "").toLowerCase().split(".").pop();
    var t = String(type || "").toLowerCase();
    if (/^(zip|7z|rar|tar|gz|bz2|xz|zst|cab)$/.test(ext)) return { label: "Archive", icon: "folder" };
    if (t.indexOf("audio/") === 0 || /^(mp3|wav|ogg|flac|m4a|opus|aac)$/.test(ext)) return { label: "Audio", icon: "speaker" };
    if (t.indexOf("video/") === 0 || /^(mp4|mkv|webm|mov|avi)$/.test(ext)) return { label: "Video", icon: "screen" };
    if (t.indexOf("image/") === 0) return { label: "Picture", icon: "picture" };
    if (/^(exe|msi|bat|cmd|ps1|vbs|js|jar|scr|com)$/.test(ext)) return { label: "Program", icon: "warn" };
    return { label: "File", icon: "download" };
  }

  // Before sending a file of `size` bytes: "" when the server's limits allow it, else why not (the server checks too).
  function fileProblem(size, storage) {
    if (!storage) return "";
    if (storage.files === false) return "This party keeps no files.";
    if (storage.limit && size > storage.limit) return "Files can be up to " + humanSize(storage.limit) + " on this server.";
    if (storage.cap && storage.used + size > storage.cap) {
      return "This server's storage is full (" + humanSize(storage.used) + " of " + humanSize(storage.cap) +
        "). The owner or an admin can delete files or raise the cap.";
    }
    if (typeof storage.free === "number" && size + MIN_FREE > storage.free) return "The host's disk is nearly full.";
    return "";
  }

  // What the meter under the server's name says.
  function storageText(storage) {
    if (!storage || storage.files === false) return "";
    if (storage.cap) return "Storage " + humanSize(storage.used) + " of " + humanSize(storage.cap);
    var text = "Storage " + humanSize(storage.used) + " (no cap)";
    if (typeof storage.free === "number") text += ", " + humanSize(storage.free) + " free on the host";
    return text;
  }

  function hasFiles(event) {
    return Boolean(event.dataTransfer && Array.prototype.indexOf.call(event.dataTransfer.types, "Files") >= 0);
  }

  // Files dragged onto the window. The whole window takes the drop, so a picture dropped on the screens or the channel list
  // is sent too, and a file dropped anywhere never makes the window open it in place of the party. While files are being
  // dragged over, <body> has the class "dropping", which the page uses to say what a drop will do.
  function dropTarget(target, onDrop) {
    var doc = target.ownerDocument || target;
    var depth = 0;
    function clear() {
      depth = 0;
      doc.body.classList.remove("dropping");
    }
    target.addEventListener("dragenter", function (event) {
      if (!hasFiles(event)) return;
      depth += 1;
      doc.body.classList.add("dropping");
    });
    target.addEventListener("dragleave", function (event) {
      if (!hasFiles(event)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) doc.body.classList.remove("dropping");
    });
    target.addEventListener("dragover", function (event) {
      if (!hasFiles(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    });
    target.addEventListener("drop", function (event) {
      clear();
      if (!hasFiles(event)) return;
      event.preventDefault();
      onDrop(Array.prototype.slice.call(event.dataTransfer.files));
    });
    if (doc.defaultView) doc.defaultView.addEventListener("blur", clear);
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
    var muted = new Set(); // text channel ids this person muted on this server (only on this PC)
    var storage = null; // the server's storage numbers and limits (server.py storage_info)
    var myUser = null; // this person's user id, to know which messages are theirs to delete
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

    // A page in another server's place in the one window is hidden too, though the browser does not say so.
    function isHidden() {
      return options.isHidden ? options.isHidden() : document.hidden;
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
      muted = server ? readMuted(store(), server.id) : new Set();
      state.meId = msg.you.id;
      myUser = msg.you.user || null;
      if (msg.storage) storage = msg.storage;
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
        if (isHidden()) buf.unread += 1;
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
      if (canDelete(message)) {
        var del = document.createElement("button");
        del.type = "button";
        del.className = "icon msg-delete";
        del.title = "Delete this message";
        del.setAttribute("aria-label", "Delete this message");
        if (typeof PPNIcons !== "undefined") del.append(PPNIcons.node("trash")); else del.textContent = "Delete";
        del.addEventListener("click", function () { deleteMessage(message); });
        head.append(del);
      }
      var text = document.createElement("div");
      text.className = "text";
      text.textContent = message.text;
      body.append(head);
      if (message.text) body.append(text);
      if (message.image) body.append(pictureNode(message));
      if (message.file) body.append(fileNode(message));
      line.dataset.id = message.id;
      line.append(options.avatar(message.name, senderKey(message)), body);
      return line;
    }

    // The sender, the owner and admins may delete a message (the server checks it again).
    function canDelete(message) {
      if (!message.id || state.role === "guest") return false;
      return isStaff() || message.from === state.meId || (myUser !== null && message.user === myUser);
    }

    function deleteMessage(message) {
      var what = message.file ? "this message and its file" : message.image ? "this message and its picture" : "this message";
      if (!window.confirm("Delete " + what + " for everyone? Copies people already saved on their own PCs stay theirs.")) return;
      options.post("/delete", { channel: message.channel, id: message.id }).catch(function () {
        options.notice("That message could not be deleted.");
      });
    }

    // A file in the chat: what it is, its size, and Save, which always saves it (it never opens in the window).
    function fileNode(message) {
      var file = message.file;
      var kind = fileKind(file.name, file.type);
      var card = document.createElement("div");
      card.className = "file-card";
      var mark = document.createElement("span");
      mark.className = "file-icon";
      if (typeof PPNIcons !== "undefined") mark.append(PPNIcons.node(kind.icon));
      var info = document.createElement("div");
      info.className = "file-info";
      var name = document.createElement("span");
      name.className = "file-name";
      name.textContent = file.name;
      name.title = file.name;
      var size = document.createElement("span");
      size.className = "file-size muted";
      size.textContent = kind.label + ", " + humanSize(file.size);
      info.append(name, size);
      var save = document.createElement("a");
      save.className = "button secondary small";
      save.href = "/file?channel=" + encodeURIComponent(message.channel) + "&message=" + encodeURIComponent(message.id);
      save.setAttribute("download", file.name);
      save.textContent = "Save";
      save.title = "Save " + file.name + " to this PC";
      card.append(mark, info, save);
      return card;
    }

    // A file on its way up: a card at the foot of the chat with its progress and a way to stop it.
    function pendingNode(file) {
      var card = document.createElement("div");
      card.className = "file-card pending";
      var info = document.createElement("div");
      info.className = "file-info";
      var name = document.createElement("span");
      name.className = "file-name";
      name.textContent = file.name;
      var bar = document.createElement("div");
      bar.className = "file-progress";
      var fill = document.createElement("span");
      bar.append(fill);
      var size = document.createElement("span");
      size.className = "file-size muted";
      size.textContent = "Sending, " + humanSize(file.size);
      info.append(name, bar, size);
      var stop = document.createElement("button");
      stop.type = "button";
      stop.className = "secondary small";
      stop.textContent = "Stop";
      card.append(info, stop);
      return { card: card, fill: fill, size: size, stop: stop };
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

    // Sending a picture: save it here at its own size, upload it, then post a message that names it. One that does not
    // fit in 12 MB at its own size goes as a file instead, so it is never made smaller.
    function sendPicture(file) {
      if (state.role === "guest" || !state.current) return Promise.resolve();
      var caption = el.chatText.value.trim();
      return preparePicture(file)
        .catch(function (err) {
          if (options.uploadFile && file.size && !(storage && storage.files === false)) return { asFile: true };
          throw err;
        })
        .then(function (prepared) {
          if (prepared.asFile) return sendFile(file);
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

    el.chatText.addEventListener("paste", function (event) {
      var file = pictureFrom((event.clipboardData && event.clipboardData.files) || []);
      if (!file) return;
      event.preventDefault();
      sendPicture(file);
    });

    // Any other file: checked against the server's limits here first, then sent as it is, with its progress showing.
    // The host keeps it; the message appears for everyone once it has all arrived.
    function sendFile(file) {
      if (state.role === "guest" || !state.current) return Promise.resolve();
      if (!options.uploadFile) {
        options.notice("Only pictures can be sent here.");
        return Promise.resolve();
      }
      if (!file.size) {
        options.notice(file.name + " is empty, so it was not sent.");
        return Promise.resolve();
      }
      var problem = fileProblem(file.size, storage);
      if (problem) {
        options.notice(file.name + " was not sent. " + problem);
        return Promise.resolve();
      }
      var pending = pendingNode(file);
      el.messages.append(pending.card);
      el.messages.scrollTop = el.messages.scrollHeight;
      var upload = options.uploadFile(file, state.current, function (sent, total) {
        var share = total ? sent / total : 0;
        pending.fill.style.width = Math.round(share * 100) + "%";
        pending.size.textContent = "Sending, " + humanSize(sent) + " of " + humanSize(total);
      });
      pending.stop.addEventListener("click", function () { upload.abort(); });
      return upload.promise
        .catch(function (err) {
          if (err && err.name !== "AbortError") options.notice(file.name + " did not send. " + ((err && err.message) || ""));
        })
        .finally(function () { pending.card.remove(); });
    }

    // Files dragged onto the window go to the channel that is open, one message each, in the order dropped: pictures
    // as pictures, anything else as a file.
    function dropped(files) {
      if (state.role === "guest") {
        options.notice("Guests can read the chat but not post.");
        return;
      }
      var chosen = files.slice(0, MAX_DROPPED);
      if (files.length > chosen.length) {
        options.notice("Only " + MAX_DROPPED + " files are sent from one drop; the other " + (files.length - chosen.length) + " were not.");
      }
      chosen.reduce(function (before, file) {
        return before.then(function () { return PICTURE_TYPES.test(file.type) ? sendPicture(file) : sendFile(file); });
      }, Promise.resolve());
    }
    if (el.dropZone) dropTarget(el.dropZone, dropped);

    function renderMessages() {
      renderComposer(); // the message box names the channel it writes to
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
      var channel = channelById(state.current);
      el.chatText.placeholder = guest ? "Guests can read the chat but not post" : channel ? "Message #" + channel.name : "Message this channel";
      var hint = el.chatForm.ownerDocument.getElementById("dropChannel");
      if (hint) hint.textContent = channel ? "#" + channel.name : "the chat";
    }

    // --- the channel list ----------------------------------------------------------------------------------

    function occupants(channelId) {
      var people = [];
      state.members.forEach(function (m) {
        if (m.voice === channelId && m.role !== "viewer") people.push(m);
      });
      return people;
    }

    function store() {
      if (options.storage) return options.storage;
      try {
        return typeof window !== "undefined" ? window.localStorage : null;
      } catch (err) {
        return null; // storage can be blocked; a mute then lasts for this visit
      }
    }

    function isMuted(id) {
      return muted.has(String(id));
    }

    function toggleMute(channel) {
      var id = String(channel.id);
      if (muted.has(id)) muted.delete(id); else muted.add(id);
      if (server) writeMuted(store(), server.id, muted);
      renderChannels();
      return muted.has(id);
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
        var silent = !isVoice && isMuted(channel.id);
        item.className = "channel " + channel.kind + (channel.id === state.current ? " current" : "") + (silent ? " muted" : "");
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
        // The page's icon set draws a # or a speaker; without it (tests, other pages) the kind is written in words.
        var glyph = typeof PPNIcons !== "undefined" ? PPNIcons.node(isVoice ? "speaker" : "hash") : null;
        label.textContent = glyph ? channel.name : (isVoice ? "Voice  " : "#  ") + channel.name;
        if (glyph) row.append(glyph);
        row.append(label);
        item.setAttribute("aria-label", (isVoice ? "Voice channel " : "Text channel ") + channel.name + (silent ? ", muted" : ""));
        if (silent && typeof PPNIcons !== "undefined") {
          var bell = PPNIcons.node("bell-off");
          if (bell) { bell.classList.add("muted-mark"); row.append(bell); }
        }
        var unread = buffer(channel.id).unread;
        if (showsUnread(channel, unread, state.current, silent)) {
          var badge = document.createElement("span");
          badge.className = "unread";
          badge.textContent = unread > 99 ? "99+" : String(unread);
          row.append(badge);
        }
        // Voice is joined with this button only, never by clicking the channel's name: a stray click must not open the mic.
        if (isVoice && state.role !== "guest") {
          var inHere = state.voice === channel.id;
          var joinButton = smallButton(inHere ? "Leave" : "Join", inHere ? "Leave this voice channel" : "Join this voice channel",
            function () { options.onVoice(inHere ? null : channel.id, channel.name); });
          joinButton.classList.add("join");
          if (inHere) joinButton.classList.add("in");
          row.append(joinButton);
        }
        if (!isVoice) {
          row.append(smallButton(silent ? "Unmute" : "Mute", silent ? "Show this channel's unread count again"
            : "Stop counting unread messages here (only for you, on this PC)", function () { toggleMute(channel); }));
          // Right-click is the quick way to the same thing.
          row.addEventListener("contextmenu", function (event) {
            event.preventDefault();
            options.notice((toggleMute(channel) ? "Muted #" : "Unmuted #") + channel.name + ". Right-click it again to undo.");
          });
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
              li.dataset.id = person.id;
              var who = document.createElement("span");
              who.className = "who";
              who.textContent = person.name + (person.id === state.meId ? " (you)" : "");
              if (options.avatar) li.append(options.avatar(person.name, person.id));
              li.append(who);
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
      muted: function () { return Array.from(muted); },
      // what the one window's rail shows: unread messages in the text channels that are not muted
      unreadTotal: function () {
        return textChannels().reduce(function (sum, c) { return sum + (isMuted(c.id) ? 0 : buffer(c.id).unread); }, 0);
      },
      // the person is looking again: what arrived in the open channel meanwhile is read
      seen: function () {
        if (state.current !== null && buffer(state.current).unread) {
          buffer(state.current).unread = 0;
          renderChannels();
        }
      },
      channelName: function (id) { var c = channelById(id); return c ? c.name : ""; },
      setStorage: function (info) { storage = info || null; },
      storage: function () { return storage; },
      // someone deleted a message: it goes from the list (copies saved on PCs stay there)
      deleted: function (msg) {
        var buf = state.buffers.get(msg.channel);
        if (!buf) return;
        var before = buf.list.length;
        buf.list = buf.list.filter(function (m) { return m.id !== msg.id; });
        if (buf.list.length !== before && msg.channel === state.current) renderMessages();
      },
    };
  }

  // Muted channels, per server, in this PC's storage for the page's address. A muted channel's messages still arrive and
  // show when it is opened; it only stops showing a count. Damaged or missing storage gives no muted channels.
  function mutedKey(serverId) {
    return "ppn.muted." + serverId;
  }

  function readMuted(storage, serverId) {
    try {
      var list = JSON.parse((storage && storage.getItem(mutedKey(serverId))) || "[]");
      if (!Array.isArray(list)) return new Set();
      return new Set(list.filter(function (x) { return typeof x === "string" || typeof x === "number"; }).map(String));
    } catch (err) {
      return new Set();
    }
  }

  function writeMuted(storage, serverId, set) {
    try {
      if (!storage) return;
      if (set.size) storage.setItem(mutedKey(serverId), JSON.stringify(Array.from(set)));
      else storage.removeItem(mutedKey(serverId));
    } catch (err) {
      // not saved; the mute still applies for this visit
    }
  }

  // A count shows on a text channel that has unread messages, is not the one open, and is not muted.
  function showsUnread(channel, unread, current, isMutedChannel) {
    return channel.kind !== "voice" && unread > 0 && channel.id !== current && !isMutedChannel;
  }

  var api = { create: create, encodingPlan: encodingPlan, preparePicture: preparePicture, dropTarget: dropTarget, hasFiles: hasFiles,
              readMuted: readMuted, writeMuted: writeMuted, showsUnread: showsUnread, humanSize: humanSize,
              fileKind: fileKind, fileProblem: fileProblem, storageText: storageText, MAX_BYTES: MAX_BYTES,
              MAX_DROPPED: MAX_DROPPED };
  if (typeof window !== "undefined") window.PartyChat = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})();
