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
  };

  var selectedPeer = null;
  var lastJoinState = "";
  var nameEditing = false;

  function api() {
    return window.pywebview.api;
  }

  function say(text) {
    el.notice.textContent = text || "";
    el.notice.hidden = !text;
  }

  function render(state) {
    el.version.textContent = "Version " + state.version;
    renderReach(state.reach);
    renderGame(state.game);
    renderRelay(state.relay, state.friend_relay);
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
    if (document.activeElement !== el.port && !state.running) el.port.value = state.port;
    el.port.disabled = state.running;
    el.toggle.textContent = state.running ? "Stop party" : "Start party";
    el.toggle.className = state.running ? "danger" : "primary";

    el.party.hidden = !state.running;
    el.serverPassword.textContent = state.passcode || "";
    if (state.join && state.join.message) {
      el.askNote.textContent = state.join.message;
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
    api().ask_join(selectedPeer).then(function (result) { el.askNote.textContent = result.message; });
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
        say(result.message);
      });
    });
  });
  el.openFolder.addEventListener("click", function () { api().open_data_folder(); });
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
    el.backupNote.textContent = result.message || "";
  }

  el.openMessages.addEventListener("click", function () {
    api().open_messages();
  });

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

  function renderReach(reach) {
    if (!reach) {
      el.reach.textContent = "";
      return;
    }
    if (reach.state === "checking") {
      el.reach.textContent = "Checking how people outside your network could reach you...";
    } else if (reach.state === "failed") {
      el.reach.textContent = "Could not check how you can be reached from outside.";
    } else {
      el.reach.textContent = reach.advice;
    }
  }

  el.copyInvite.addEventListener("click", function () {
    api().invite().then(function (result) {
      if (!result.ok) {
        el.inviteNote.textContent = result.message;
        return;
      }
      var done = function () { el.inviteNote.textContent = "Copied. Send it to the person; it contains your party's password."; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(result.invite).then(done, function () {
          el.inviteCode.value = result.invite;
          el.inviteNote.textContent = "Copy did not work here. The code is in the box below; select it and copy.";
        });
      } else {
        el.inviteCode.value = result.invite;
        el.inviteNote.textContent = "The code is in the box below; select it and copy.";
      }
    });
  });

  var joinTimer = null;

  function showJoin(status) {
    var busy = status.state === "trying" || status.state === "waiting";
    el.joinStatus.hidden = !status.message || status.state === "idle";
    el.joinStatus.textContent = status.message || "";
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
    el.gameStatus.textContent = game.message || "";
    el.gameOff.hidden = !game.active;
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
    if (friend && document.activeElement !== el.friendRelay && document.activeElement !== el.friendRelayMbps) {
      el.friendRelay.checked = friend.on;
      el.friendRelayMbps.value = friend.mbps;
      el.friendRelayMbps.disabled = !friend.on;
    }
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
