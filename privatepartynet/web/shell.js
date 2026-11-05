/*
 * The party room's frame: the Chat and Members tabs of the right panel, and the sidebar that slides over the stage in a
 * narrow window. It changes what is shown and nothing else; the chat, the roster and the screens are room.js's.
 */
(function () {
  "use strict";

  var tabChat = document.getElementById("tabChat");
  var tabMembers = document.getElementById("tabMembers");
  var chat = document.getElementById("chat");
  var people = document.getElementById("people");
  var messages = document.getElementById("messages");
  if (!tabChat || !tabMembers || !chat || !people) return;

  function showTab(name) {
    var members = name === "members";
    chat.hidden = members;
    people.hidden = !members;
    tabChat.setAttribute("aria-selected", String(!members));
    tabMembers.setAttribute("aria-selected", String(members));
    if (!members) tabChat.classList.remove("attn");
  }

  tabChat.addEventListener("click", function () { showTab("chat"); });
  tabMembers.addEventListener("click", function () { showTab("members"); });
  [tabChat, tabMembers].forEach(function (tab) {
    tab.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      var other = tab === tabChat ? tabMembers : tabChat;
      other.focus();
      other.click();
      event.preventDefault();
    });
  });

  // A message that arrives while Members is showing marks the Chat tab.
  if (messages && window.MutationObserver) {
    new MutationObserver(function () {
      if (tabMembers.getAttribute("aria-selected") === "true") tabChat.classList.add("attn");
    }).observe(messages, { childList: true });
  }

  // Panels that can be made wider or narrower, and sections that fold (panels.js). Not in a pop-out window.
  if (window.PPNPanels && !document.body.classList.contains("viewer")) {
    PPNPanels.resizable(document.getElementById("sidebar"), { side: "right", variable: "--sidebar-w", min: 200, max: 440,
      key: "room.sidebar", label: "channel list", leave: 440 });
    PPNPanels.resizable(document.getElementById("side"), { side: "left", variable: "--side-w", min: 260, max: 640,
      key: "room.side", label: "chat and members", leave: 440 });
    PPNPanels.collapsible(document.querySelector("#channelsBox h2"), document.getElementById("channelList"),
      { key: "room.channels", label: "Channels" });
    PPNPanels.collapsible(document.querySelector("#mine h2"), document.getElementById("myShares"),
      { key: "room.myshares", label: "Your screens" });
  }

  // In a narrow window the sidebar is a drawer over the stage. A click on the stage, or Escape, closes it.
  var sidebarBtn = document.getElementById("sidebarBtn");
  var center = document.getElementById("center");
  function setDrawer(open) {
    document.body.classList.toggle("sidebar-open", open);
    if (sidebarBtn) sidebarBtn.setAttribute("aria-expanded", String(open));
  }
  if (sidebarBtn) {
    sidebarBtn.setAttribute("aria-expanded", "false");
    sidebarBtn.addEventListener("click", function (event) {
      event.stopPropagation();
      setDrawer(!document.body.classList.contains("sidebar-open"));
    });
  }
  if (center) {
    center.addEventListener("click", function () {
      if (document.body.classList.contains("sidebar-open")) setDrawer(false);
    });
  }
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) setDrawer(false);
  });
  // Choosing a text channel on a narrow window closes the drawer so the chat is in view.
  var channelList = document.getElementById("channelList");
  if (channelList) {
    channelList.addEventListener("click", function (event) {
      if (!event.target.closest(".channel.text")) return;
      showTab("chat");
      setDrawer(false);
    });
  }
})();
