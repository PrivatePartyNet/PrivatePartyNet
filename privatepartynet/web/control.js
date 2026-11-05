/*
 * The control panel. Runs inside the app's native window and talks to the Python side through the bridge
 * (window.pywebview.api). Names and addresses are written with textContent, never as HTML: they come from
 * other PCs on the network.
 */
(function () {
  "use strict";

  var el = {
    version: document.getElementById("version"),
    name: document.getElementById("name"),
    port: document.getElementById("port"),
    toggle: document.getElementById("toggle"),
    notice: document.getElementById("notice"),
    party: document.getElementById("party"),
    others: document.getElementById("others"),
    room: document.getElementById("room"),
    peers: document.querySelector("#peers tbody"),
    nobody: document.getElementById("nobody"),
    ask: document.getElementById("ask"),
    askNote: document.getElementById("askNote"),
    askPasswordRow: document.getElementById("askPasswordRow"),
    askPassword: document.getElementById("askPassword"),
    askWithPassword: document.getElementById("askWithPassword"),
    autoUpdate: document.getElementById("autoUpdate"),
    testVersions: document.getElementById("testVersions"),
    checkUpdates: document.getElementById("checkUpdates"),
    updateStatus: document.getElementById("updateStatus"),
    updateCard: document.getElementById("updateCard"),
    updateText: document.getElementById("updateText"),
    updateNotes: document.getElementById("updateNotes"),
    updateButtons: document.getElementById("updateButtons"),
    requests: document.getElementById("requests"),
    requestList: document.getElementById("requestList"),
    themeItems: document.querySelectorAll("#optionsMenu [data-choice]"),
    openFolder: document.getElementById("openFolder"),
    reach: document.getElementById("reach"),
    serverPassword: document.getElementById("serverPassword"),
    newServerPassword: document.getElementById("newServerPassword"),
    saveServerPassword: document.getElementById("saveServerPassword"),
    relayNow: document.getElementById("relayNow"),
    relayAddress: document.getElementById("relayAddress"),
    relaySecret: document.getElementById("relaySecret"),
    relayPin: document.getElementById("relayPin"),
    saveRelay: document.getElementById("saveRelay"),
    clearRelay: document.getElementById("clearRelay"),
    friendRelay: document.getElementById("friendRelay"),
    friendRelayMbps: document.getElementById("friendRelayMbps"),
    friendRelayNow: document.getElementById("friendRelayNow"),
    pathProblems: document.getElementById("pathProblems"),
    pathWays: document.getElementById("pathWays"),
    copyInvite: document.getElementById("copyInvite"),
    inviteNote: document.getElementById("inviteNote"),
    inviteCode: document.getElementById("inviteCode"),
    joinInvite: document.getElementById("joinInvite"),
    joinCancel: document.getElementById("joinCancel"),
    joinAddress: document.getElementById("joinAddress"),
    joinPort: document.getElementById("joinPort"),
    joinPassword: document.getElementById("joinPassword"),
    joinAddressBtn: document.getElementById("joinAddressBtn"),
    joinStatus: document.getElementById("joinStatus"),
    joined: document.getElementById("joined"),
    gameList: document.getElementById("gameList"),
    gameNone: document.getElementById("gameNone"),
    gameStatus: document.getElementById("gameStatus"),
    gameOff: document.getElementById("gameOff"),
    openMessages: document.getElementById("openMessages"),
    messagesBox: document.getElementById("messagesBox"),
    messagesFrame: document.getElementById("messagesFrame"),
    dmUnread: document.getElementById("dmUnread"),
    unlockBox: document.getElementById("unlockBox"),
    unlockPassword: document.getElementById("unlockPassword"),
    unlock: document.getElementById("unlock"),
    passwordSummary: document.getElementById("passwordSummary"),
    oldPassword: document.getElementById("oldPassword"),
    newPassword: document.getElementById("newPassword"),
    savePassword: document.getElementById("savePassword"),
    backupPassphrase: document.getElementById("backupPassphrase"),
    withIdentity: document.getElementById("withIdentity"),
    exportChats: document.getElementById("exportChats"),
    importChats: document.getElementById("importChats"),
    backupNote: document.getElementById("backupNote"),
    openHow: document.getElementById("openHow"),
    openAbout: document.getElementById("openAbout"),
    addresses: document.getElementById("addresses"),
    reachChips: document.getElementById("reachChips"),
    inviteOut: document.getElementById("inviteOut"),
    userId: document.getElementById("userId"),
    gameAddress: document.getElementById("gameAddress"),
    gameAddressRow: document.getElementById("gameAddressRow"),
    gameMembers: document.getElementById("gameMembers"),
    gameMembersCard: document.getElementById("gameMembersCard"),
  };

  // How the game network reaches a member, in words (the app sends the way as a short word).
  // Same wording as WAY_TEXT in pathway.py, so this page and the paths summary agree.
  var WAY_WORDS = {
    lan: "direct on the same network", ipv6: "direct over IPv6", upnp: "direct through an open port",
    punch: "direct through both routers", friend: "through a member's PC", vps: "through the relay server",
    "": "not connected yet",
  };

  var selectedPeer = null;
  var lastJoinState = "";
  var nameEditing = false;

  function api() {
    return window.pywebview.api;
  }

  var hidden = {};  // box -> the text last hidden there, so a message repeated by every poll is logged once

  // Writes a status or error box, unless the text looks like a certificate, key, invite code or token (safetext.js):
  // the box then says one was hidden, and the app logs where.
  function put(node, text, where) {
    var kind = window.PrivatePartyNetSafeText.secretLike(text);
    if (kind && hidden[where] !== text) {
      hidden[where] = text;
      api().hidden_message(where, kind);
    }
    node.textContent = window.PrivatePartyNetSafeText.shown(text);
  }

  function say(text) {
    put(el.notice, text, "notice");
    el.notice.hidden = !text;
  }

  function render(state) {
    el.version.textContent = "Version " + state.version;
    renderReach(state.reach);
    renderGame(state.game);
    renderRelay(state.relay, state.friend_relay);
    renderUpdate(state.update);
    renderSounds(state.sounds);
    soundRequests(state.requests);
    el.unlockBox.hidden = !state.locked;
    el.openMessages.disabled = state.locked;
    el.dmUnread.textContent = state.dm_unread ? state.dm_unread + " unread" : "";
    el.passwordSummary.textContent = state.chat_password ? "Change or remove the password" : "Add a password";
    el.toggle.disabled = state.locked;
    if (document.documentElement.dataset.theme !== state.theme) {
      document.documentElement.dataset.theme = state.theme;
    }
    el.themeItems.forEach(function (item) {
      item.setAttribute("aria-checked", item.dataset.choice === state.theme ? "true" : "false");
    });
    document.querySelectorAll(".appname").forEach(function (node) {
      if (state.app_name && node.textContent !== state.app_name) node.textContent = state.app_name;
    });
    if (!nameEditing && document.activeElement !== el.name) el.name.value = state.name;
    if (document.activeElement !== el.port && (!state.running || !el.port.value)) el.port.value = state.port;
    el.port.disabled = state.running;
    el.toggle.textContent = state.running ? "Stop party" : "Start party";
    el.toggle.className = state.running ? "danger" : "primary";

    el.party.hidden = !state.running;
    el.serverPassword.textContent = state.passcode || "";
    renderAddresses(state.addresses || []);
    el.userId.textContent = state.user_id || "";
    // A join asked from the Nearby list answers there; an invite's join answers under the invite box, not here as well.
    var nearbyJoin = state.join && state.join.peer;
    el.askPasswordRow.hidden = !(nearbyJoin && state.join.needs_password);
    if (nearbyJoin) passwordPeer = state.join.peer;
    if (state.join && state.join.message) {
      if (nearbyJoin) put(el.askNote, state.join.message, "ask note");
      if (state.join.state !== lastJoinState) {
        lastJoinState = state.join.state;
        if (state.join.state !== "trying" && state.join.state !== "waiting") loadJoined();
      }
    }
    el.others.textContent = state.others === 1 ? "1 person besides you" : state.others + " people besides you";

    renderPeers(state.peers);
    renderRequests(state.requests);

    var message = state.notice || state.problem || "";
    if (message) say(message);
  }

  function renderPeers(peers) {
    el.peers.textContent = "";
    el.nobody.hidden = peers.length > 0;
    if (selectedPeer && !peers.some(function (p) { return p.id === selectedPeer; })) selectedPeer = null;
    peers.forEach(function (peer) {
      var row = document.createElement("tr");
      row.dataset.id = peer.id;
      if (peer.id === selectedPeer) row.className = "selected";
      [peer.name, peer.address, peer.state].forEach(function (text) {
        var cell = document.createElement("td");
        cell.textContent = text;
        row.append(cell);
      });
      row.addEventListener("click", function () {
        selectedPeer = peer.id;
        renderPeers(peers);
        el.ask.disabled = false;
      });
      el.peers.append(row);
    });
    el.ask.disabled = !selectedPeer;
  }

  function renderRequests(requests) {
    el.requests.hidden = requests.length === 0;
    el.requestList.textContent = "";
    requests.forEach(function (request) {
      var item = document.createElement("li");
      var text = document.createElement("span");
      text.textContent = request.name + " (" + request.address + ") wants to join your party";
      var yes = document.createElement("button");
      yes.className = "primary";
      yes.textContent = "Let in";
      var no = document.createElement("button");
      no.className = "secondary";
      no.textContent = "No";
      yes.addEventListener("click", function () { decide(request.id, true); });
      no.addEventListener("click", function () { decide(request.id, false); });
      item.append(text, yes, no);
      el.requestList.append(item);
    });
  }

  function decide(id, accept) {
    api().decide(id, accept).then(function (result) { say(result.message); });
  }

  el.name.addEventListener("focus", function () { nameEditing = true; });
  el.name.addEventListener("blur", function () { nameEditing = false; });
  // Every edit goes out at once, so the network sees the name as it is typed.
  el.name.addEventListener("input", function () {
    api().rename(el.name.value);
  });

  el.toggle.addEventListener("click", function () {
    if (el.toggle.textContent === "Stop party") {
      api().stop().then(function (result) { say(result.message); });
      return;
    }
    api().start(el.name.value, Number(el.port.value)).then(function (result) {
      say(result.message);
    });
  });

  el.room.addEventListener("click", function () {
    api().open_room().then(function (result) { say(result.message); });
  });

  el.ask.addEventListener("click", function () {
    if (!selectedPeer) return;
    api().ask_join(selectedPeer, "").then(function (result) { put(el.askNote, result.message, "ask note"); });
  });

  // A party seen over a VPN (Radmin VPN, say) counts that PC as outside its network, so it wants its password.
  var passwordPeer = null;
  el.askWithPassword.addEventListener("click", function () {
    var peer = passwordPeer || selectedPeer;
    if (!peer) return;
    api().ask_join(peer, el.askPassword.value).then(function (result) {
      put(el.askNote, result.message, "ask note");
      if (result.ok) el.askPasswordRow.hidden = true;
    });
  });

  // --- the menu bar ------------------------------------------------------------------------------------------

  var menuButtons = Array.from(document.querySelectorAll(".menubtn"));

  function closeMenus() {
    menuButtons.forEach(function (button) {
      button.setAttribute("aria-expanded", "false");
      document.getElementById(button.getAttribute("aria-controls")).hidden = true;
    });
  }

  function openMenu(button) {
    closeMenus();
    button.setAttribute("aria-expanded", "true");
    var menu = document.getElementById(button.getAttribute("aria-controls"));
    menu.hidden = false;
    var first = menu.querySelector("button");
    if (first) first.focus();
  }

  menuButtons.forEach(function (button) {
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      if (button.getAttribute("aria-expanded") === "true") {
        closeMenus();
      } else {
        openMenu(button);
      }
    });
    // Moving along the bar while a menu is open switches menus, as in any desktop program.
    button.addEventListener("mouseenter", function () {
      if (menuButtons.some(function (other) { return other.getAttribute("aria-expanded") === "true"; })) openMenu(button);
    });
  });

  document.addEventListener("click", closeMenus);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeMenus();
    var open = document.querySelector(".dropdown:not([hidden])");
    if (!open || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) return;
    var items = Array.from(open.querySelectorAll("button"));
    var at = items.indexOf(document.activeElement);
    var next = event.key === "ArrowDown" ? (at + 1) % items.length : (at - 1 + items.length) % items.length;
    items[next].focus();
    event.preventDefault();
  });

  el.themeItems.forEach(function (item) {
    item.addEventListener("click", function () {
      api().set_theme(item.dataset.choice).then(function (result) {
        document.documentElement.dataset.theme = item.dataset.choice;
        if (shell()) shell().theme(item.dataset.choice); // the rail and every room follow at once
        say(result.message);
      });
    });
  });
  el.openFolder.addEventListener("click", function () { api().open_data_folder(); });

  // --- sounds (the one window plays them; Settings sets how loud and which) -------------------------------------

  var soundVolume = document.getElementById("soundVolume");
  var soundVolumeV = document.getElementById("soundVolumeV");
  var soundSwitches = Array.from(document.querySelectorAll("[data-sound]"));
  var soundSaveTimer = null;
  var soundTouched = 0;

  function soundChoice() {
    return { volume: Number(soundVolume.value), off: soundSwitches.filter(function (s) { return !s.checked; }).map(function (s) { return s.dataset.sound; }) };
  }

  function renderSounds(sounds) {
    if (!sounds || !soundVolume || Date.now() - soundTouched < 2000) return; // not while it is being changed
    soundVolume.value = sounds.volume;
    soundVolumeV.textContent = sounds.volume + "%";
    soundSwitches.forEach(function (s) { s.checked = sounds.off.indexOf(s.dataset.sound) < 0; });
  }

  function soundChanged() {
    soundTouched = Date.now();
    var choice = soundChoice();
    soundVolumeV.textContent = choice.volume + "%";
    if (shell()) shell().setSounds(choice);
    clearTimeout(soundSaveTimer);
    soundSaveTimer = setTimeout(function () { api().set_sounds(choice.volume, choice.off); }, 300);
  }

  if (soundVolume) {
    soundVolume.addEventListener("input", soundChanged);
    soundSwitches.forEach(function (s) { s.addEventListener("change", soundChanged); });
    document.querySelectorAll("[data-play]").forEach(function (button) {
      button.addEventListener("click", function () { if (shell()) shell().sound(button.dataset.play, true); });
    });
  }

  // Someone new asking through Nearby PCs is heard, as a request in a room is.
  var knownRequests = null;
  function soundRequests(list) {
    var ids = new Set((list || []).map(function (r) { return r.id; }));
    if (knownRequests !== null && Array.from(ids).some(function (id) { return !knownRequests.has(id); }) && shell()) shell().sound("request");
    knownRequests = ids;
  }

  // --- updates (updater.py finds them; updates.js decides what the banner says) ---------------------------------

  var lastUpdate = "";

  function renderUpdate(view) {
    if (!view) return;
    el.autoUpdate.checked = Boolean(view.automatic);
    el.testVersions.checked = Boolean(view.include_test);
    var shown = window.PrivatePartyNetUpdates.card(view);
    var key = JSON.stringify(shown);
    if (key === lastUpdate) return;
    lastUpdate = key;
    el.updateCard.hidden = !shown.show;
    el.updateText.textContent = shown.text;
    el.updateNotes.textContent = shown.notes;
    el.updateNotes.hidden = !shown.notes;
    el.updateButtons.textContent = "";
    shown.buttons.forEach(function (spec) {
      var button = document.createElement("button");
      button.className = spec.primary ? "primary" : "secondary";
      button.textContent = spec.label;
      button.disabled = spec.disabled;
      button.addEventListener("click", function () {
        var call = { restart: "update_restart", later: "update_later", skip: "update_skip" }[spec.action];
        api()[call]().then(function (result) { if (result && result.message && !result.ok) say(result.message); });
      });
      el.updateButtons.append(button);
    });
  }

  [[el.autoUpdate, "auto_update"], [el.testVersions, "update_prerelease"]].forEach(function (pair) {
    pair[0].addEventListener("change", function () {
      api().set_update_option(pair[1], pair[0].checked).then(function (result) {
        if (result.update) renderUpdate(result.update);
      });
    });
  });
  el.checkUpdates.addEventListener("click", function () {
    el.updateStatus.textContent = "Checking...";
    el.checkUpdates.disabled = true;
    api().check_updates().then(function (view) {
      renderUpdate(view);
      put(el.updateStatus, window.PrivatePartyNetUpdates.card(view).show ? "" : view.message, "update status");
    }).catch(function () {
      el.updateStatus.textContent = "The check did not finish.";
    }).then(function () {
      el.checkUpdates.disabled = false;
    });
  });
  el.openHow.addEventListener("click", function () {
    api().open_how().then(function (result) { say(result.message); });
  });
  el.openAbout.addEventListener("click", function () {
    api().open_about().then(function (result) { say(result.message); });
  });

  function poll() {
    api().state().then(render).catch(function () {
      // the bridge is not ready yet, or the window is closing; the next poll tries again
    }).finally(function () {
      setTimeout(poll, 700);
    });
  }

  window.addEventListener("pywebviewready", poll);

  function backupSay(result) {
    put(el.backupNote, result.message, "backup note");
  }

  el.openMessages.addEventListener("click", function () {
    api().open_messages();
  });

  // The one window's own controls (appshell.js), looked up when needed: its script may finish after this page's.
  function shell() {
    try {
      return window.top !== window && window.top.PPNShell ? window.top.PPNShell : null;
    } catch (err) {
      return null;
    }
  }

  // Inside the one window, the Direct messages page shows the conversations (dm.html) instead of a button for a window.
  if (document.documentElement.classList.contains("in-shell") && el.messagesFrame) {
    el.messagesBox.hidden = true;
    el.messagesFrame.hidden = false;
    el.messagesFrame.src = "dm.html?embedded=1";
  }

  el.unlock.addEventListener("click", function () {
    api().unlock(el.unlockPassword.value).then(function (result) {
      say(result.ok ? "Unlocked." : result.message);
      if (result.ok) el.unlockPassword.value = "";
    });
  });

  el.savePassword.addEventListener("click", function () {
    api().set_chat_password(el.newPassword.value, el.oldPassword.value || null).then(function (result) {
      say(result.ok ? (el.newPassword.value ? "Password saved." : "Password removed.") : result.message);
      if (result.ok) {
        el.oldPassword.value = "";
        el.newPassword.value = "";
      }
    });
  });

  el.exportChats.addEventListener("click", function () {
    backupSay({ message: "Saving..." });
    api().export_chats(el.backupPassphrase.value, el.withIdentity.checked).then(backupSay);
  });

  el.importChats.addEventListener("click", function () {
    backupSay({ message: "Reading the file..." });
    api().import_chats(el.backupPassphrase.value, el.withIdentity.checked).then(backupSay);
  });

  function chip(text, kind) {
    var node = document.createElement("span");
    node.className = "badge" + (kind ? " " + kind : "");
    node.textContent = text;
    return node;
  }

  function renderReach(reach) {
    el.reachChips.textContent = "";
    if (!reach) {
      el.reach.textContent = "";
      return;
    }
    if (reach.state === "checking") {
      el.reach.textContent = "Checking how people outside your network could reach you...";
    } else if (reach.state === "failed") {
      el.reach.textContent = "Could not check how you can be reached from outside.";
    } else {
      put(el.reach, reach.advice, "reach");
      // The facts behind the advice, as short labels.
      // reach.reachable is "yes", "maybe" or "no"; reach.nat is "easy", "hard" or "unknown"
      var outside = { yes: ["Reachable from outside", "good"], maybe: ["Maybe reachable from outside", "warn"] }[reach.reachable] ||
        ["Not reachable from outside", "bad"];
      el.reachChips.append(
        chip(outside[0], outside[1]),
        chip(reach.port_open ? "Router port open" : "Router port not opened", reach.port_open ? "good" : ""),
        chip(reach.ipv6 ? "IPv6 available" : "No IPv6", reach.ipv6 ? "good" : ""),
        chip(reach.cgnat ? "Carrier-grade NAT" : "No carrier-grade NAT", reach.cgnat ? "warn" : "good")
      );
      var nat = { easy: ["Router easy to punch through", "good"], hard: ["Router gives a new port per destination", "warn"] }[reach.nat];
      if (nat) el.reachChips.append(chip(nat[0], nat[1]));
    }
  }

  // The addresses other PCs on this network can use to reach the party.
  function renderAddresses(list) {
    var key = list.join("|");
    if (el.addresses.dataset.key === key) return;
    el.addresses.dataset.key = key;
    el.addresses.textContent = "";
    list.forEach(function (address) {
      var item = document.createElement("li");
      item.textContent = address;
      el.addresses.append(item);
    });
  }

  el.copyInvite.addEventListener("click", function () {
    api().invite().then(function (result) {
      if (!result.ok) {
        put(el.inviteNote, result.message, "invite note");
        return;
      }
      // The code stays in a box under the button, so it can be selected and copied by hand if the clipboard is refused.
      el.inviteOut.value = result.invite;
      el.inviteOut.hidden = false;
      var done = function () { el.inviteNote.textContent = "Copied. Send it to the person; it contains your party's password."; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(result.invite).then(done, function () {
          el.inviteNote.textContent = "Copy did not work here. The code is in the box below; select it and copy.";
        });
      } else {
        el.inviteNote.textContent = "The code is in the box below; select it and copy.";
      }
    });
  });

  var joinTimer = null;

  function showJoin(status) {
    var busy = status.state === "trying" || status.state === "waiting";
    el.joinStatus.hidden = !status.message || status.state === "idle";
    put(el.joinStatus, status.message, "join status");
    el.joinCancel.hidden = !busy;
    el.joinInvite.disabled = el.joinAddressBtn.disabled = busy;
    if (!busy && joinTimer) {
      clearInterval(joinTimer);
      joinTimer = null;
      loadJoined();
    }
  }

  function watchJoin() {
    if (joinTimer) return;
    joinTimer = setInterval(function () { api().join_status().then(showJoin); }, 1000);
  }

  el.joinInvite.addEventListener("click", function () {
    api().join_invite(el.inviteCode.value).then(function (result) {
      if (!result.ok) {
        showJoin({ state: "failed", message: result.message });
        return;
      }
      showJoin({ state: "trying", message: result.message });
      watchJoin();
    });
  });

  el.joinAddressBtn.addEventListener("click", function () {
    api().join_address(el.joinAddress.value, el.joinPort.value, el.joinPassword.value, "").then(function (result) {
      if (!result.ok) {
        showJoin({ state: "failed", message: result.message });
        return;
      }
      showJoin({ state: "trying", message: result.message });
      watchJoin();
    });
  });

  el.joinCancel.addEventListener("click", function () {
    api().join_cancel();
  });

  function loadJoined() {
    api().joined_servers().then(function (list) {
      el.joined.textContent = "";
      list.forEach(function (server) {
        var li = document.createElement("li");
        var name = document.createElement("span");
        name.textContent = server.name;
        var again = document.createElement("button");
        again.className = "secondary";
        again.textContent = "Ask to join again";
        again.addEventListener("click", function () {
          api().rejoin(server.key).then(function (result) {
            showJoin({ state: result.ok ? "trying" : "failed", message: result.message });
            if (result.ok) watchJoin();
          });
        });
        var forget = document.createElement("button");
        forget.className = "secondary";
        forget.textContent = "Forget";
        forget.addEventListener("click", function () { api().forget_joined(server.key).then(loadJoined); });
        li.append(name, again, forget);
        el.joined.append(li);
      });
    });
  }

  el.saveServerPassword.addEventListener("click", function () {
    api().set_server_password(el.newServerPassword.value).then(function (result) {
      say(result.message);
      if (result.ok) el.newServerPassword.value = "";
    });
  });

  var lastGame = "";

  function renderGame(game) {
    if (!game) return;
    var key = JSON.stringify(game);
    if (key === lastGame) return;
    lastGame = key;
    el.gameList.textContent = "";
    el.gameNone.hidden = game.networks.length > 0;
    game.networks.forEach(function (network) {
      var li = document.createElement("li");
      var light = document.createElement("span");
      var active = game.active === network.key;
      light.className = "light" + (active && game.state === "on" ? " on" : active ? " starting" : "");
      light.title = active && game.state === "on" ? "In use for games" : active ? "Starting" : "Not in use";
      var name = document.createElement("span");
      name.textContent = network.name + (active && game.address ? "  (" + game.address + ")" : "");
      li.append(light, name);
      // the light's meaning in words too, so it does not rest on color alone
      if (active) {
        var inUse = document.createElement("span");
        inUse.className = "badge " + (game.state === "on" ? "good" : "warn");
        inUse.textContent = game.state === "on" ? "In use for games" : "Starting";
        li.append(inUse);
      }
      if (!active) {
        var use = document.createElement("button");
        use.className = "secondary";
        use.textContent = "Use for games";
        use.addEventListener("click", function () {
          api().game_use(network.key).then(function (result) { if (!result.ok) say(result.message); });
        });
        li.append(use);
      }
      el.gameList.append(li);
    });
    put(el.gameStatus, game.message, "game network");
    el.gameOff.hidden = !game.active;
    var on = game.active && game.address;
    el.gameAddressRow.hidden = !on;
    el.gameAddress.textContent = on ? game.address + (game.network ? "  (network " + game.network + ")" : "") : "";
    el.gameMembers.textContent = "";
    (game.members || []).forEach(function (member) {
      var li = document.createElement("li");
      var where = document.createElement("span");
      where.className = "mono-text";
      where.textContent = member.address;
      var way = document.createElement("span");
      way.className = "muted";
      way.textContent = WAY_WORDS[member.way || ""] || member.way;
      li.append(where, way);
      el.gameMembers.append(li);
    });
    updateMembersCard();
  }

  // The members card shows when there is a member to list or a path to explain; the relay summary fills the lines in it.
  function updateMembersCard() {
    el.gameMembersCard.hidden = !el.gameMembers.children.length && el.pathWays.hidden && el.pathProblems.hidden;
  }

  el.gameOff.addEventListener("click", function () {
    api().game_off();
  });

  function renderRelay(relay, friend) {
    if (relay) {
      var where = relay.host.indexOf(":") >= 0 ? "[" + relay.host + "]" : relay.host;
      el.relayNow.textContent = "In use: " + where + ":" + relay.port + (relay.pin ? ", certificate checked." : ".");
    } else {
      el.relayNow.textContent = "No relay server is set.";
    }
    el.clearRelay.hidden = !relay;
    if (!friend) return;
    if (document.activeElement !== el.friendRelay && document.activeElement !== el.friendRelayMbps) {
      el.friendRelay.checked = friend.on;
      el.friendRelayMbps.value = friend.mbps;
      el.friendRelayMbps.disabled = !friend.on;
    }
    var through = friend.through || [];
    el.friendRelayNow.textContent = through.length ? "Going through this PC now: " + through.join(", ") + "." : "";
    el.friendRelayNow.hidden = !through.length;
    var ways = friend.ways || [];
    el.pathWays.textContent = ways.length ? "Game network: " + ways.join("; ") + "." : "";
    el.pathWays.hidden = !ways.length;
    var problems = friend.problems || [];
    put(el.pathProblems, problems.length ? "Cannot reach " + problems.join(" ") : "", "game network paths");
    el.pathProblems.hidden = !problems.length;
    updateMembersCard();
  }

  el.saveRelay.addEventListener("click", function () {
    api().set_relay(el.relayAddress.value, el.relaySecret.value, el.relayPin.value).then(function (result) {
      say(result.message);
      if (result.ok) el.relaySecret.value = ""; // it is kept on this PC now; the page never shows it again
    });
  });

  el.clearRelay.addEventListener("click", function () {
    api().clear_relay().then(function (result) { say(result.message); });
  });

  function saveFriendRelay() {
    api().set_friend_relay(el.friendRelay.checked, el.friendRelayMbps.value).then(function (result) {
      if (!result.ok) say(result.message);
    });
  }

  el.friendRelay.addEventListener("change", saveFriendRelay);
  el.friendRelayMbps.addEventListener("change", saveFriendRelay);

  window.addEventListener("pywebviewready", loadJoined);
})();
