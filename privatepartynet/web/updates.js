/*
 * What the update prompt in the control panel, the Updates card in Settings and the Updates part of the About page say,
 * from the app's update view (updater.py, Updater.view). Pure functions, so the tests run them in Node; the pages write
 * the result with textContent, never as HTML (release notes come from GitHub).
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

  // The prompt card: { show, text, notes, buttons: [{ action, label, primary, disabled }], bar }. `bar` is the
  // download's share (0 to 1) while it comes in, else null (progress).
  function card(view) {
    var none = { show: false, text: "", notes: "", buttons: [], bar: null };
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
        bar: null,
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
        bar: null,
      };
    }
    if (view.state === "downloading" || view.state === "verifying" || view.state === "installing" ||
        view.state === "manual") {
      return { show: true, text: String(view.message || ""), notes: "", buttons: [], bar: progress(view).bar };
    }
    return none;
  }

  // A download under way: { show, text, bar }. While bytes come in, `bar` is their share (0 to 1) of the file and the
  // text says how much, how fast and how long is left (updater.py, downloading_message); while the file is checked
  // against GitHub's SHA-256, or unpacked, there is no bar.
  function progress(view) {
    if (!view || typeof view !== "object") return { show: false, text: "", bar: null };
    if (view.state === "downloading") {
      var figures = view.download && typeof view.download === "object" ? view.download : {};
      var share = Number(figures.total) > 0 ? Number(figures.done) / Number(figures.total) : Number(view.progress);
      return { show: true, text: String(view.message || ""), bar: isFinite(share) ? Math.min(1, Math.max(0, share)) : 0 };
    }
    if (view.state === "verifying") {
      return { show: true, text: String(view.message || "Checking the download..."), bar: null };
    }
    return { show: false, text: "", bar: null };
  }

  // The Updates card in Settings, on every refresh, whether or not Check for updates was pressed: { text, bar,
  // checked }. Empty text before any check, and when the version offered was set aside (Later, Skip).
  function status(view) {
    if (!view || typeof view !== "object") return { text: "", bar: null, checked: "" };
    var shown = progress(view);
    var quiet = view.state === "idle" || view.state === "dismissed";
    return { text: shown.show ? shown.text : quiet ? "" : String(view.message || ""), bar: shown.bar,
             checked: about(view).checked };
  }

  // True while a check or a download (and its check against GitHub's SHA-256) is under way: the pages ask more often.
  function busy(view) {
    return !!view && (view.state === "checking" || view.state === "downloading" || view.state === "verifying");
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

  return { card: card, about: about, busy: busy, progress: progress, status: status };
});
