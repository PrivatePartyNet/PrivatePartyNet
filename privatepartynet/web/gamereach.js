/*
 * The control panel's "What party members can reach on this PC" menu, under Game network. Each group of Windows
 * services (file sharing, Remote Desktop, ...) is closed to party members unless it is ticked here; the app passes the
 * change to the game network helper at once. rows() is a pure function so the tests can run it in Node; the rest fills
 * in #gameReachList in the control panel.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PrivatePartyNetGameReach = api;
    api.start(root);
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // What each row shows, from the app's {groups: [{id, label, detail, open}]}. Anything malformed is left out.
  function rows(answer) {
    var groups = answer && Array.isArray(answer.groups) ? answer.groups : [];
    return groups
      .filter(function (g) { return g && typeof g.id === "string" && typeof g.label === "string"; })
      .map(function (g) {
        return { id: g.id, label: g.label, detail: typeof g.detail === "string" ? g.detail : "", open: g.open === true };
      });
  }

  function render(list, answer, onToggle) {
    var doc = list.ownerDocument;
    list.textContent = "";
    rows(answer).forEach(function (row) {
      var label = doc.createElement("label");
      label.className = "check";
      var box = doc.createElement("input");
      box.type = "checkbox";
      box.checked = row.open;
      box.dataset.group = row.id;
      box.addEventListener("change", function () { onToggle(row.id, box.checked, box); });
      var text = doc.createElement("span");
      var name = doc.createElement("strong");
      name.textContent = row.label;
      var detail = doc.createElement("span");
      detail.className = "muted";
      detail.textContent = " " + row.detail;
      text.append(name, doc.createElement("br"), detail);
      label.append(box, text);
      list.append(label);
    });
  }

  function start(win) {
    var list = win.document.getElementById("gameReachList");
    if (!list) return;
    function api() { return win.pywebview.api; }
    function toggle(group, open, box) {
      box.disabled = true;
      api().game_set_service(group, open).then(function (answer) {
        if (answer && answer.ok) render(list, answer, toggle);
        else box.checked = !open;
        box.disabled = false;
      });
    }
    win.addEventListener("pywebviewready", function () {
      api().game_services().then(function (answer) { render(list, answer, toggle); });
    });
  }

  return { rows: rows, render: render, start: start };
});
