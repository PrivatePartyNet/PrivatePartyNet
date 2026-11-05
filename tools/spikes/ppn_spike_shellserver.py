"""Spike only: serve the stand-in shell page on 127.0.0.1:<port> for <seconds>, then exit. Usage: py ppn_spike_shellserver.py PORT SECONDS"""
import http.server
import sys
import threading

SHELL = b"""<!doctype html><html><head><meta charset="utf-8"><title>shell</title></head><body style="margin:0;background:#111">
<script>
window.__msgs = [];
window.addEventListener("message", function (e) {
  var frame = Array.prototype.find.call(document.querySelectorAll("iframe"), function (f) { return f.contentWindow === e.source; });
  window.__msgs.push({ origin: e.origin, frame: frame ? frame.dataset.i : null, data: e.data });
});
var params = new URLSearchParams(location.search);
var urls = (params.get("frames") || "").split("|").filter(Boolean);
urls.forEach(function (u, i) {
  var f = document.createElement("iframe");
  f.dataset.i = String(i);
  if (params.get("noallow") !== "1") f.setAttribute("allow", "microphone; fullscreen; clipboard-write; autoplay");
  f.style.cssText = "border:0;width:100vw;height:100vh;display:" + (i === 0 ? "block" : "none");
  f.src = u;
  document.body.append(f);
});
window.show = function (i) {
  document.querySelectorAll("iframe").forEach(function (f) { f.style.display = f.dataset.i === String(i) ? "block" : "none"; });
};
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(SHELL)))
        self.end_headers()
        self.wfile.write(SHELL)

    def log_message(self, *_):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
threading.Timer(float(sys.argv[2]), server.shutdown).start()
print("SHELL READY", flush=True)
server.serve_forever()
