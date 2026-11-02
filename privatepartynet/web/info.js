/*
 * The About and How pages. Both fill the app name and version from the Python side and follow the chosen theme.
 * Values from the app are written with textContent, never as HTML.
 */
(function () {
  "use strict";

  var FACTS = [
    ["version", "Version"],
    ["packaged", "Installed build"],
    ["windows", "Windows"],
    ["webview2", "WebView2 runtime"],
    ["python", "Python"],
    ["app_folder", "App folder"],
    ["data_folder", "Data folder"],
    ["log_file", "Log file"],
  ];

  function fill(info) {
    document.documentElement.dataset.theme = info.theme;
    document.querySelectorAll(".appname").forEach(function (node) { node.textContent = info.name; });
    document.querySelectorAll(".appversion").forEach(function (node) { node.textContent = info.version; });
    document.title = document.title.replace("PrivatePartyNet", info.name);
    if (document.getElementById("facts")) fillAbout(info);
  }

  function fillAbout(info) {
    var facts = document.getElementById("facts");
    var lines = [info.name + " " + info.version];
    facts.textContent = "";
    FACTS.forEach(function (pair) {
      var value = pair[0] === "packaged" ? (info.packaged ? "Yes" : "No, running from source") : info[pair[0]];
      var term = document.createElement("dt");
      term.textContent = pair[1];
      var detail = document.createElement("dd");
      detail.textContent = value;
      facts.append(term, detail);
      lines.push(pair[1] + ": " + value);
    });
    var body = document.querySelector("#libs tbody");
    body.textContent = "";
    info.libraries.forEach(function (lib) {
      var row = document.createElement("tr");
      [lib.name, lib.version, lib.purpose].forEach(function (text) {
        var cell = document.createElement("td");
        cell.textContent = text;
        row.append(cell);
      });
      body.append(row);
      lines.push(lib.name + " " + lib.version);
    });
    var copy = document.getElementById("copy");
    copy.addEventListener("click", function () { copyText(lines.join("\n")); });
  }

  // A page opened from a file has no secure context, so the clipboard API may be missing; the textarea route works anywhere.
  function copyText(text) {
    var done = document.getElementById("copied");
    var area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }
    area.remove();
    done.textContent = ok ? "Copied." : "Could not copy. Select the text and copy it by hand.";
    setTimeout(function () { done.textContent = ""; }, 4000);
  }

  window.addEventListener("pywebviewready", function () {
    window.pywebview.api.about_info().then(fill).catch(function () {
      // opened without the app behind it: the page keeps its built-in text
    });
  });
})();
