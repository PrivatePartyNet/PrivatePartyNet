/*
 * One of the app's own pages (control, direct messages, About, How) shown inside the one window. The app puts its bridge
 * (window.pywebview) into the window's top page only; these pages come from the same origin as that page, so they use
 * its bridge as their own and are told when it is ready, exactly as they would be in a window of their own. A page
 * opened any other way (as a file, in its own window, under another origin) is left as it is.
 */
(function () {
  "use strict";

  var top = null;
  try {
    if (window.top !== window && window.top.document.documentElement.hasAttribute("data-ppn-shell")) top = window.top;
  } catch (err) {
    top = null; // another origin above: nothing is borrowed
  }
  if (!top) return;
  document.documentElement.classList.add("in-shell");

  var done = false;
  function borrow() {
    if (done || !top.pywebview || !top.pywebview.api) return false;
    done = true;
    window.pywebview = top.pywebview;
    window.dispatchEvent(new Event("pywebviewready"));
    return true;
  }

  function begin() {
    if (!borrow()) top.addEventListener("pywebviewready", function () { setTimeout(borrow, 0); });
  }

  // after the page's own scripts have added their "pywebviewready" listeners
  if (document.readyState === "complete") setTimeout(begin, 0);
  else window.addEventListener("load", function () { setTimeout(begin, 0); });
})();
