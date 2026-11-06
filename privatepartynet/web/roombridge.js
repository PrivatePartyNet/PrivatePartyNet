/*
 * A server's room shown inside the one window (?embedded=1&shell=http://127.0.0.1:PORT). The room is on its server's
 * own address, so it cannot use the window's bridge to the app directly. This gives it window.pywebview.api with the
 * calls a room may make; each call goes to the shell page by postMessage, addressed to the shell's origin only, and its
 * answer comes back the same way, accepted only from the frame's parent at that origin. Whatever else the shell says
 * (the person's look, the theme, whether this server is shown, a voice command, leave) arrives as a "ppn-shell" event
 * for room.js. A room opened anywhere else (a test browser) is left as it is.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root && root.document) api.install(root);
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var METHODS = ["close_window", "set_fullscreen", "keep_chat", "keep_blob", "appearance", "my_picture", "server_look"];
  var TIMEOUT_MS = 20000;

  // The shell's origin from the room's address, or "" when the room is not inside the one window.
  function shellOrigin(search) {
    var params = new URLSearchParams(search || "");
    var shell = params.get("shell") || "";
    if (params.get("embedded") !== "1" || !/^http:\/\/127\.0\.0\.1:\d{1,5}$/.test(shell)) return "";
    return shell;
  }

  function install(win) {
    var shell = shellOrigin(win.location.search);
    if (!shell || win.parent === win) return;
    var waiting = new Map();
    var next = 0;

    function send(msg) {
      win.parent.postMessage(msg, shell);
    }

    function call(method, args) {
      return new Promise(function (resolve, reject) {
        next += 1;
        var id = next;
        var timer = setTimeout(function () {
          waiting.delete(id);
          reject(new Error("The app did not answer."));
        }, TIMEOUT_MS);
        waiting.set(id, { resolve: resolve, reject: reject, timer: timer });
        send({ ppn: "call", id: id, method: method, args: Array.prototype.slice.call(args) });
      });
    }

    var api = {};
    METHODS.forEach(function (method) {
      api[method] = function () { return call(method, arguments); };
    });
    win.pywebview = { api: api };
    win.PPNEmbedded = { shell: shell, send: send };
    win.document.documentElement.classList.add("embedded");

    win.addEventListener("message", function (event) {
      if (event.source !== win.parent || event.origin !== shell) return;
      var msg = event.data;
      if (!msg || typeof msg !== "object" || typeof msg.ppn !== "string") return;
      if (msg.ppn === "result") {
        var waiter = waiting.get(msg.id);
        if (!waiter) return;
        waiting.delete(msg.id);
        clearTimeout(waiter.timer);
        if (msg.ok) waiter.resolve(msg.value);
        else waiter.reject(new Error(msg.error || "refused"));
        return;
      }
      win.dispatchEvent(new CustomEvent("ppn-shell", { detail: msg }));
    });

    function ready() {
      win.dispatchEvent(new Event("pywebviewready"));
    }
    if (win.document.readyState === "complete") setTimeout(ready, 0);
    else win.addEventListener("load", function () { setTimeout(ready, 0); });
  }

  return { METHODS: METHODS, shellOrigin: shellOrigin, install: install };
});
