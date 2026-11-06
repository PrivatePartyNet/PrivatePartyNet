/*
 * The room's frame: panels that resize and fold, and the sidebar that slides over the middle in a narrow window. It
 * changes what is shown and nothing else; the chat, the members and the screens are room.js's.
 */
(function () {
  "use strict";

  // Panels that can be made wider or narrower, and sections that fold (panels.js).
  if (window.PPNPanels) {
    PPNPanels.resizable(document.getElementById("sidebar"), { side: "right", variable: "--sidebar-w", min: 200, max: 440,
      key: "room.sidebar", label: "channel list", leave: 440 });
    PPNPanels.resizable(document.getElementById("side"), { side: "left", variable: "--side-w", min: 200, max: 480,
      key: "room.side", label: "member list", leave: 440 });
    PPNPanels.collapsible(document.querySelector("#channelsBox h2"), document.getElementById("channelList"),
      { key: "room.channels", label: "Channels" });
    PPNPanels.collapsible(document.querySelector("#mine h2"), document.getElementById("myShares"),
      { key: "room.myshares", label: "Your screens" });
  }

  // In a narrow window the sidebar is a drawer over the middle. A click on the middle, or Escape, closes it.
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
  // Choosing a channel on a narrow window closes the drawer so the channel is in view.
  var channelList = document.getElementById("channelList");
  if (channelList) {
    channelList.addEventListener("click", function (event) {
      var row = event.target.closest(".channel");
      if (!row || event.target.closest("button")) return;
      setDrawer(false);
    });
  }
})();
