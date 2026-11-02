/*
 * The direct messages window. Runs in the app's native window and talks to the Python side through the bridge
 * (window.pywebview.api). Names and message text come from other PCs, so everything is written with textContent.
 * Messages are sealed to the other person's key before they leave this PC; see dm.py.
 */
(function () {
  "use strict";

  var POLL_MS = 1500;
  var el = {
    contacts: document.getElementById("contacts"),
    noContacts: document.getElementById("noContacts"),
    nearby: document.getElementById("nearby"),
    servers: document.getElementById("servers"),
    noServers: document.getElementById("noServers"),
    noNearby: document.getElementById("noNearby"),
    head: document.getElementById("head"),
    who: document.getElementById("who"),
    presence: document.getElementById("presence"),
    safety: document.getElementById("safety"),
    safetyNumber: document.getElementById("safetyNumber"),
    pick: document.getElementById("pick"),
    messages: document.getElementById("messages"),
    composer: document.getElementById("composer"),
    text: document.getElementById("text"),
    attach: document.getElementById("attach"),
    file: document.getElementById("file"),
    note: document.getElementById("note"),
    lightbox: document.getElementById("lightbox"),
  };
  var current = null; // the contact id whose conversation is open
  var reading = null; // { server, channel, title } while a saved server channel is open (read only)
  var lastServers = "";
  var lastState = "";
  var lastMessages = "";
  var pictures = new Map(); // hash -> data address
  var contactsById = new Map();

  function api() {
    return window.pywebview.api;
  }

  function say(text) {
    el.note.textContent = text || "";
  }

  function item(text, extra) {
    var li = document.createElement("li");
    var name = document.createElement("span");
    name.className = "name";
    name.textContent = text;
    li.append(name);
    if (extra) li.append(extra);
    return li;
  }

  function renderState(state) {
    var key = JSON.stringify(state);
    if (key === lastState) return;
    lastState = key;
    contactsById = new Map(state.contacts.map(function (c) { return [c.id, c]; }));
    el.contacts.textContent = "";
    el.noContacts.hidden = state.contacts.length > 0;
    state.contacts.forEach(function (contact) {
      var li = item(contact.name);
      li.className = "contact" + (contact.id === current ? " current" : "") + (contact.online ? " online" : "");
      li.title = contact.preview;
      var dot = document.createElement("span");
      dot.className = "presence";
      li.prepend(dot);
      if (contact.new) {
        var fresh = document.createElement("span");
        fresh.className = "tag";
        fresh.textContent = "New";
        li.append(fresh);
      }
      if (contact.unread) {
        var badge = document.createElement("span");
        badge.className = "unread";
        badge.textContent = contact.unread > 99 ? "99+" : String(contact.unread);
        li.append(badge);
      }
      li.addEventListener("click", function () { open(contact.id); });
      el.contacts.append(li);
    });
    el.nearby.textContent = "";
    el.noNearby.hidden = state.nearby.length > 0;
    state.nearby.forEach(function (person) {
      var add = document.createElement("button");
      add.className = "secondary small";
      add.textContent = "Add";
      add.addEventListener("click", function () {
        api().dm_add(person.id).then(function (result) {
          if (!result.ok) say(result.message);
          refresh();
        });
      });
      var li = item(person.name, add);
      li.title = person.address;
      el.nearby.append(li);
    });
    if (current && contactsById.has(current)) {
      var chosen = contactsById.get(current);
      el.presence.textContent = chosen.online ? "on the network now" : "not seen on the network";
    }
  }

  function renderServers(list) {
    var key = JSON.stringify(list);
    if (key === lastServers) return;
    lastServers = key;
    el.servers.textContent = "";
    el.noServers.hidden = list.length > 0;
    list.forEach(function (server) {
      server.channels.forEach(function (channel) {
        var li = item(server.name + "  #" + channel.name);
        li.className = "contact" + (reading && reading.server === server.server && reading.channel === channel.id ? " current" : "");
        li.addEventListener("click", function () { openSaved(server, channel); });
        el.servers.append(li);
      });
    });
  }

  function openSaved(server, channel) {
    current = null;
    reading = { server: server.server, channel: channel.id };
    lastMessages = "";
    lastServers = "";
    el.safetyNumber.hidden = true;
    say("");
    el.who.textContent = server.name + "  #" + channel.name;
    el.presence.textContent = "saved on this PC (read only)";
    el.safety.hidden = true;
    el.head.hidden = el.messages.hidden = false;
    el.composer.hidden = true;
    el.pick.hidden = true;
    refresh();
  }

  function open(id) {
    current = id;
    reading = null;
    el.safety.hidden = false;
    lastState = "";
    lastMessages = "";
    el.safetyNumber.hidden = true;
    say("");
    var contact = contactsById.get(id);
    el.who.textContent = contact ? contact.name : "";
    el.head.hidden = el.messages.hidden = el.composer.hidden = false;
    el.pick.hidden = true;
    api().dm_read(id);
    refresh();
    el.text.focus();
  }

  function pictureNode(hash) {
    var image = document.createElement("img");
    image.className = "dm-image";
    image.alt = "Picture";
    function show(src) {
      image.src = src;
    }
    if (pictures.has(hash)) {
      show(pictures.get(hash));
    } else {
      api().dm_image(hash).then(function (result) {
        if (result.ok) {
          pictures.set(hash, result.src);
          show(result.src);
        }
      });
    }
    image.addEventListener("click", function () {
      if (!image.src) return;
      el.lightbox.querySelector("img").src = image.src;
      el.lightbox.hidden = false;
    });
    return image;
  }

  function renderMessages(list) {
    var key = JSON.stringify(list);
    if (key === lastMessages) return;
    lastMessages = key;
    var stick = el.messages.scrollHeight - el.messages.scrollTop - el.messages.clientHeight < 80 || !el.messages.childElementCount;
    el.messages.textContent = "";
    list.forEach(function (m) {
      var row = document.createElement("div");
      row.className = "message " + (m.mine ? "mine" : "theirs");
      var bubble = document.createElement("div");
      bubble.className = "bubble";
      if (m.text) {
        var text = document.createElement("div");
        text.className = "text";
        text.textContent = m.text;
        bubble.append(text);
      }
      if (m.image) bubble.append(pictureNode(m.image));
      if (reading && !m.mine) {
        var who = document.createElement("div");
        who.className = "who";
        who.textContent = m.name;
        bubble.prepend(who);
      }
      var meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = new Date(m.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) +
        (m.pending ? "  sending..." : "") + (m.refused ? "  not delivered: their PC refused it" : "");
      bubble.append(meta);
      row.append(bubble);
      el.messages.append(row);
    });
    if (stick) el.messages.scrollTop = el.messages.scrollHeight;
  }

  function refresh() {
    api().dm_state().then(function (state) {
      if (!state.ok) {
        say(state.message);
        return;
      }
      renderState(state);
      api().saved_servers().then(renderServers);
      if (reading) {
        api().saved_history(reading.server, reading.channel).then(function (result) {
          if (result.ok) renderMessages(result.messages);
        });
      } else if (current) {
        api().dm_history(current).then(function (result) {
          if (result.ok) renderMessages(result.messages);
        });
        var contact = contactsById.get(current);
        if (contact && contact.unread) api().dm_read(current);
      }
    });
  }

  el.composer.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = el.text.value.trim();
    if (!current || !text) return;
    el.text.value = "";
    api().dm_send(current, text, null).then(function (result) {
      if (!result.ok) say(result.message);
      refresh();
    });
  });

  function readAsBase64(blob) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result).split(",")[1]); };
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
  }

  function sendPicture(file) {
    if (!current) return;
    PartyChat.preparePicture(file)
      .then(function (prepared) { return readAsBase64(prepared.blob); })
      .then(function (data) { return api().dm_send(current, el.text.value.trim(), data); })
      .then(function (result) {
        if (!result.ok) say(result.message);
        else el.text.value = "";
        refresh();
      })
      .catch(function (err) { say(err && err.message ? err.message : "That picture did not send."); });
  }

  el.attach.addEventListener("click", function () { el.file.click(); });
  el.file.addEventListener("change", function () {
    var file = el.file.files[0];
    el.file.value = "";
    if (file) sendPicture(file);
  });
  el.text.addEventListener("paste", function (event) {
    var files = (event.clipboardData && event.clipboardData.files) || [];
    for (var i = 0; i < files.length; i += 1) {
      if (/^image\/(png|jpeg|gif|webp)$/.test(files[i].type)) {
        event.preventDefault();
        sendPicture(files[i]);
        return;
      }
    }
  });

  el.safety.addEventListener("click", function () {
    if (!current) return;
    if (!el.safetyNumber.hidden) {
      el.safetyNumber.hidden = true;
      return;
    }
    api().dm_safety(current).then(function (result) {
      el.safetyNumber.textContent = result.ok
        ? "Safety number: " + result.number + ". Read it to them, or have them read theirs. If the two match, nobody is in between."
        : "";
      el.safetyNumber.hidden = !result.ok;
    });
  });

  el.lightbox.addEventListener("click", function () { el.lightbox.hidden = true; });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") el.lightbox.hidden = true;
  });

  function start() {
    api().about_info().then(function (info) {
      if (info.theme && info.theme !== "system") document.documentElement.dataset.theme = info.theme;
      document.querySelectorAll(".appname").forEach(function (node) { node.textContent = info.name; });
    });
    refresh();
    setInterval(refresh, POLL_MS);
  }

  if (window.pywebview && window.pywebview.api) start();
  else window.addEventListener("pywebviewready", start);
})();
