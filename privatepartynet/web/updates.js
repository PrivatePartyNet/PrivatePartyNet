/*
 * What the update prompt in the control panel and the Updates part of the About page say, from the app's update view
 * (updater.py, Updater.view). Pure functions, so the tests run them in Node; the pages write the result with
 * textContent, never as HTML (release notes come from GitHub).
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PrivatePartyNetUpdates = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // The prompt card: { show, text, notes, buttons: [{ action, label, primary, disabled }] }.
  function card(view) {
    var none = { show: false, text: "", notes: "", buttons: [] };
    if (!view || typeof view !== "object") return none;
    var version = String(view.version || "");
    var name = "PrivatePartyNet " + version;
    var notes = typeof view.notes === "string" ? view.notes : "";
    if (view.state === "ready") {
      var waiting = String(view.waiting || "");
      return {
        show: true,
        // As updater.py's ready_message: nothing restarts by itself; closing the app installs it.
        text: waiting
          ? name + " is ready. Restart now works once " + waiting + " ends, or it installs when you close PrivatePartyNet."
          : "Ready: Restart now, or " + name + " installs when you close PrivatePartyNet.",
        notes: notes,
        buttons: [
          { action: "restart", label: "Restart now", primary: true, disabled: !!waiting },
          { action: "later", label: "Later", primary: false, disabled: false },
        ],
      };
    }
    if (view.state === "available") {
      var runsFromSource = view.kind === "source";
      return {
        show: true,
        text: name + " is out." + (runsFromSource ? " This copy runs from source: update it with git." : ""),
        notes: notes,
        buttons: runsFromSource ? [{ action: "later", label: "Later", primary: false, disabled: false }] : [
          { action: "restart", label: "Update", primary: true, disabled: false },
          { action: "later", label: "Later", primary: false, disabled: false },
          { action: "skip", label: "Skip this version", primary: false, disabled: false },
        ],
      };
    }
    if (view.state === "downloading" || view.state === "installing" || view.state === "manual") {
      return { show: true, text: String(view.message || ""), notes: "", buttons: [] };
    }
    return none;
  }

  // The About page's lines: { status, checked }.
  function about(view, now) {
    if (!view || typeof view !== "object") return { status: "", checked: "" };
    var checked = "Not checked yet.";
    if (typeof view.checked_at === "number" && view.checked_at > 0) {
      checked = "Last checked " + when(view.checked_at, now) + ".";
    }
    var status = String(view.message || "");
    if (!status) status = view.automatic ? "Automatic updates are on." : "Automatic updates are off.";
    return { status: status, checked: checked };
  }

  function when(seconds, now) {
    var ago = Math.max(0, Math.round(((now || Date.now()) / 1000 - seconds) / 60));
    if (ago < 1) return "just now";
    if (ago < 60) return ago + (ago === 1 ? " minute ago" : " minutes ago");
    var hours = Math.round(ago / 60);
    if (hours < 48) return hours + (hours === 1 ? " hour ago" : " hours ago");
    return new Date(seconds * 1000).toLocaleDateString();
  }

  return { card: card, about: about };
});
