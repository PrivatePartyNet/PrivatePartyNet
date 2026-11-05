// The one window, end to end, headless: the real shell page (served by shellserver.py) with a stand-in for the app's
// bridge in its top page, a real party (serve_for_e2e.py), the host's own room and a member's HTTPS room in frames.
// Usage: node tests/browser/e2e_shell.mjs  (Python from PRIVATEPARTYNET_PYTHON, Edge found on this PC or from
// PRIVATEPARTYNET_EDGE; E2E_SHOTS=<folder> also saves pictures). Nothing shows on screen. A watchdog ends it all.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = process.env.PRIVATEPARTYNET_PYTHON || "python";
const EDGE = process.env.PRIVATEPARTYNET_EDGE || ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe"].find(existsSync);
const SHOTS = process.env.E2E_SHOTS || "";
const PORT = 8819, DEBUG_PORT = 9341, RUN_LIMIT_MS = 300_000, SERVER_SECONDS = 290;
const children = [];
let failed = 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function check(name, ok, detail = "") { console.log(`${ok ? "ok   " : "FAIL "}${name}${detail ? ` (${detail})` : ""}`); if (!ok) failed += 1; }
async function waitFor(test, ms = 20_000) {
  const end = Date.now() + ms;
  while (Date.now() < end) { try { if (await test()) return true; } catch { /* still loading */ } await sleep(300); }
  return false;
}
function killAll() { for (const c of children) if (!c.killed) c.kill(); }
setTimeout(() => { console.log("FAIL run limit"); killAll(); process.exit(1); }, RUN_LIMIT_MS).unref();
function start(cmd, args) { const c = spawn(cmd, args, { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] }); children.push(c); return c; }

function readLines(child, pattern, count, ms = 20_000) {
  return new Promise((done) => {
    let text = "";
    const timer = setTimeout(() => done(text), ms);
    child.stdout.on("data", (chunk) => {
      text += chunk;
      if ((text.match(pattern) || []).length >= count) { clearTimeout(timer); done(text); }
    });
  });
}

// The app's side, as the shell sees it: what to open and close, and the calls a room may make.
const STATE = {
  version: "3.3.0", app_name: "PrivatePartyNet", theme: "dark", name: "Kay", port: PORT, running: true,
  addresses: ["https://192.168.1.20:8765"], passcode: "tiger-maple-42", others: 1, peers: [], requests: [], join: null,
  notice: "", problem: "", locked: false, chat_password: false, user_id: "b3f1c9a27d4e8f0a6c2b5e9d1f3a7c8e", dm_unread: 2,
  reach: { state: "done", reachable: "yes", advice: "", nat: "easy", cgnat: false, ipv6: true, port_open: true },
  game: { networks: [], active: null, chosen: null, state: "off", message: "", address: "", network: "", members: [] },
  relay: null, friend_relay: { on: false, mbps: 8, through: [], problems: [], ways: [] },
  update: { automatic: true, include_test: true, state: "idle", message: "" },
};
const BRIDGE = `(function () {
  if (window.top !== window) return;
  var ok = function () { return Promise.resolve({ ok: true, message: "" }); };
  window.__events = []; window.__calls = []; window.__dm = 2; window.__theme = "dark";
  window.__look = { accent: null, text_scale: 100, density: "default" };
  window.pywebview = { api: {
    shell_state: function () { return Promise.resolve({ dm_unread: window.__dm, theme: window.__theme, appearance: window.__look,
      running: true, events: window.__events.splice(0) }); },
    appearance: function () { return Promise.resolve(window.__look); },
    room_call: function (key, method, args) {
      window.__calls.push([key, method, args]);
      if (method === "appearance") return Promise.resolve(window.__look);
      if (method === "close_window") { window.__events.push({ type: "close", key: key }); return ok(); }
      if (method === "set_fullscreen") return Promise.resolve({ ok: true, fullscreen: Boolean(args[0]) });
      return Promise.resolve({ ok: true, missing: [] });
    },
    state: function () { return Promise.resolve(${JSON.stringify(STATE)}); },
    joined_servers: function () { return Promise.resolve([]); },
    game_services: function () { return Promise.resolve({ ok: true, groups: [] }); },
    about_info: function () { return Promise.resolve({ name: "PrivatePartyNet", version: "3.3.0", theme: window.__theme,
      appearance: window.__look, libraries: [], update: null }); },
    dm_state: function () { return Promise.resolve({ ok: true, me: "me", dm_ready: true, contacts: [], nearby: [] }); },
    saved_servers: function () { return Promise.resolve([]); },
    rename: ok, set_theme: ok, set_appearance: ok, hidden_message: ok, open_room: ok, open_messages: ok
  } };
  setTimeout(function () { window.dispatchEvent(new Event("pywebviewready")); }, 50);
})();`;

class Browser {
  constructor(url) { this.url = url; this.n = 0; this.pending = new Map(); this.handlers = []; }
  async open() {
    this.ws = new WebSocket(this.url);
    await new Promise((ok, bad) => { this.ws.onopen = ok; this.ws.onerror = () => bad(new Error("ws")); });
    this.ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id && this.pending.has(m.id)) { const p = this.pending.get(m.id); this.pending.delete(m.id); m.error ? p.bad(new Error(m.error.message)) : p.ok(m.result); return; }
      for (const h of this.handlers) h(m);
    };
  }
  send(method, params = {}, sessionId) {
    const id = ++this.n;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    this.ws.send(JSON.stringify(msg));
    return new Promise((ok, bad) => { this.pending.set(id, { ok, bad }); setTimeout(() => { if (this.pending.delete(id)) bad(new Error("timeout " + method)); }, 20_000); });
  }
}

// One person: their own browser profile, the shell page, and every frame in it (other sites run in their own process).
class Person {
  constructor(browser) { this.b = browser; this.frames = []; this.contexts = new Map(); }
  async open(url) {
    const { browserContextId } = await this.b.send("Target.createBrowserContext", { disposeOnDetach: false });
    const { targetId } = await this.b.send("Target.createTarget", { url: "about:blank", browserContextId });
    const { sessionId } = await this.b.send("Target.attachToTarget", { targetId, flatten: true });
    this.s = sessionId;
    this.b.handlers.push((m) => {
      if (m.method === "Target.attachedToTarget" && m.sessionId === this.s && m.params.targetInfo.type === "iframe") {
        const child = m.params.sessionId;
        this.frames.push(child);
        this.b.send("Security.setIgnoreCertificateErrors", { ignore: true }, child).catch(() => {});
        this.b.send("Runtime.runIfWaitingForDebugger", {}, child).catch(() => {});
      }
      if (m.method === "Runtime.executionContextCreated" && m.sessionId === this.s && m.params.context.auxData && m.params.context.auxData.isDefault) {
        this.contexts.set(m.params.context.auxData.frameId, m.params.context.id);
      }
    });
    await this.b.send("Security.setIgnoreCertificateErrors", { ignore: true }, this.s).catch(() => {});
    await this.b.send("Runtime.enable", {}, this.s);
    await this.b.send("Page.enable", {}, this.s);
    await this.b.send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false }, this.s);
    await this.b.send("Page.addScriptToEvaluateOnNewDocument", { source: BRIDGE }, this.s);
    await this.b.send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: false, flatten: true }, this.s);
    await this.b.send("Page.navigate", { url }, this.s);
  }
  async top(expr) { return this.value(await this.b.send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }, this.s)); }
  value(r) { if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "eval"); return r.result.value; }
  async frame(match, expr) {
    for (const sid of this.frames) {
      const href = await this.b.send("Runtime.evaluate", { expression: "location.href", returnByValue: true }, sid).then((r) => r.result.value).catch(() => "");
      if (href.includes(match)) return this.value(await this.b.send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }, sid));
    }
    const tree = await this.b.send("Page.getFrameTree", {}, this.s);
    const all = [];
    (function walk(node) { all.push(node.frame); (node.childFrames || []).forEach(walk); })(tree.frameTree);
    const frame = all.find((f) => f.parentId && f.url.includes(match));
    if (!frame) throw new Error("no frame " + match);
    const contextId = this.contexts.get(frame.id);
    if (!contextId) throw new Error("no context " + match);
    return this.value(await this.b.send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true, contextId }, this.s));
  }
  async shot(name) {
    if (!SHOTS) return;
    mkdirSync(SHOTS, { recursive: true });
    const { data } = await this.b.send("Page.captureScreenshot", { format: "png" }, this.s);
    writeFileSync(join(SHOTS, name + ".png"), Buffer.from(data, "base64"));
  }
}

async function main() {
  const party = start(PYTHON, [join(ROOT, "tests", "browser", "serve_for_e2e.py"), String(PORT), "428617", String(SERVER_SECONDS)]);
  const said = await readLines(party, /JOIN \w+ \S+/g, 2);
  const local = Number((said.match(/READY (\d+)/) || [])[1]);
  const tokens = Object.fromEntries([...said.matchAll(/JOIN (\w+) (\S+)/g)].map((m) => [m[1], m[2]]));
  check("the party is up", Boolean(local && tokens.Sam));
  const pages = start(PYTHON, [join(ROOT, "tests", "browser", "serve_shell.py"), String(SERVER_SECONDS)]);
  const shellPort = Number(((await readLines(pages, /SHELL \d+/g, 1)).match(/SHELL (\d+)/) || [])[1]);
  check("the shell's pages are served", Boolean(shellPort));

  const profile = mkdtempSync(join(tmpdir(), "ppn-shell-edge-"));
  start(EDGE, ["--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", `--user-data-dir=${profile}`,
    `--remote-debugging-port=${DEBUG_PORT}`, "--ignore-certificate-errors", "--allow-loopback-in-peer-connection",
    "--disable-features=WebRtcHideLocalIpsWithMdns", "--autoplay-policy=no-user-gesture-required",
    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "about:blank"]);
  check("the browser is up", await waitFor(async () => (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/version`)).ok));
  const browser = new Browser((await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/version`)).json()).webSocketDebuggerUrl);
  await browser.open();
  const shellUrl = `http://127.0.0.1:${shellPort}/shell.html`;

  // --- the host: Home, then their own room on the rail -----------------------------------------------------------
  const host = new Person(browser);
  await host.open(shellUrl);
  check("the window opens on Home", await waitFor(() => host.top("document.getElementById('view-home').classList.contains('current')")));
  check("Home is the control panel, on the window's own bridge", await waitFor(() => host.frame("control.html",
    "window.pywebview === window.top.pywebview && document.documentElement.classList.contains('in-shell') && document.getElementById('name').value === 'Kay'")));
  check("the Home button counts unread direct messages", await waitFor(() => host.top("document.querySelector('#homeBtn .srv-badge').textContent === '2'")));
  check("Direct messages are a page of Home, not a window", await waitFor(() => host.frame("control.html",
    "!document.getElementById('messagesFrame').hidden && document.getElementById('messagesFrame').src.includes('dm.html')")));
  await host.shot("shell-home");

  await host.top(`__events.push({ type: "open", key: "own", name: "End-to-end party", url: "http://127.0.0.1:${local}/host", own: true, select: true }); true`);
  const hostRoom = `127.0.0.1:${local}/host`;
  check("the party's room is put on the rail and shown", await waitFor(() => host.top(
    "document.querySelectorAll('#serverList .srv-item').length === 1 && document.querySelector('#views iframe.view.current') !== document.getElementById('view-home')")));
  check("the room signs in inside the frame and knows it is embedded", await waitFor(() => host.frame(hostRoom,
    "document.documentElement.classList.contains('embedded') && document.getElementById('partyName').textContent === 'End-to-end party'")));
  check("the room's calls reach the app through the shell, for that room only", await waitFor(() => host.top(
    "__calls.some(function (c) { return c[0] === 'own' && c[1] === 'appearance'; })")));
  await host.top(`__look = { accent: "#16a34a", text_scale: 110, density: "compact" }; true`);
  check("the person's look reaches the room at once", await waitFor(() => host.frame(hostRoom,
    "document.documentElement.style.getPropertyValue('--accent').includes('#16a34a') && document.documentElement.style.getPropertyValue('--text-scale') === '1.1'")));
  check("and the rail", await host.top("document.documentElement.style.getPropertyValue('--accent').includes('#16a34a')"));
  await host.top(`__theme = "light"; true`);
  check("the person's theme reaches the room, whatever the host picked", await waitFor(() => host.frame(hostRoom, "document.documentElement.dataset.theme === 'light'")));
  await host.top(`__theme = "dark"; __look = { accent: null, text_scale: 100, density: "default" }; true`);
  await waitFor(() => host.frame(hostRoom, "document.documentElement.dataset.theme === 'dark'"));
  await host.shot("shell-own-room");

  // --- Sam: a member's HTTPS room in a frame -----------------------------------------------------------------
  const sam = new Person(browser);
  await sam.open(shellUrl);
  await waitFor(() => sam.top("!!window.PPNShell"));
  await sam.top(`__events.push({ type: "open", key: "join:a", name: "End-to-end party", url: "https://127.0.0.1:${PORT}/join?token=${tokens.Sam}", own: false, select: true }); true`);
  const samRoom = `127.0.0.1:${PORT}`;
  check("a member's HTTPS room signs in inside the frame", await waitFor(() => sam.frame(samRoom,
    "document.documentElement.classList.contains('embedded') && document.getElementById('count').textContent.startsWith('2 people')"), 30_000));

  await sam.top("PPNShell.show('home'); true");
  await host.frame(hostRoom, "document.getElementById('chatText').value = 'hello from the host'; document.getElementById('chatForm').requestSubmit(); true");
  check("a message while another view is shown marks the server unread", await waitFor(() => sam.top(
    "document.querySelector('#serverList .srv-item').classList.contains('unread')")));
  await sam.shot("shell-unread");
  await sam.top("document.querySelector('#serverList .srv').click(); true");
  check("showing it again reads it", await waitFor(() => sam.top("!document.querySelector('#serverList .srv-item').classList.contains('unread')")));

  // voice continues while Home is shown, and the rail has its controls
  await sam.frame(samRoom, "[...document.querySelectorAll('#channelList .channel.voice .channel-row button')].find(function (b) { return b.textContent === 'Join'; }).click(); true");
  check("Sam joins voice in the room", await waitFor(() => sam.frame(samRoom, "!document.getElementById('voiceBar').hidden")));
  await sam.top("PPNShell.show('home'); true");
  check("with Home shown, the call's controls are at the foot of the rail", await waitFor(() => sam.top(
    "!document.getElementById('voiceDock').hidden && !document.querySelector('#serverList .srv-mark.voice').hidden")));
  await sam.shot("shell-voice-dock");
  await sam.top("document.getElementById('dockMute').click(); true");
  check("the rail's mute mutes the call in the room", await waitFor(() => sam.frame(samRoom, "document.getElementById('muteBtn').textContent === 'Unmute'")));
  check("and the rail shows it muted", await waitFor(() => sam.top("document.getElementById('dockMute').getAttribute('aria-pressed') === 'true'")));
  await sam.top("document.getElementById('dockLeave').click(); true");
  check("the rail's leave ends the call", await waitFor(() => sam.frame(samRoom, "document.getElementById('voiceBar').hidden")));
  check("and the controls go", await waitFor(() => sam.top("document.getElementById('voiceDock').hidden")));

  // fullscreen hides the rail for that room
  await sam.top("document.querySelector('#serverList .srv').click(); true");
  await sam.frame(samRoom, "document.getElementById('fullBtn').click(); true");
  check("fullscreen asks the app and hides the rail", await waitFor(() => sam.top(
    "document.getElementById('shell').classList.contains('focus') && __calls.some(function (c) { return c[1] === 'set_fullscreen' && c[2][0] === true; })")));
  await sam.frame(samRoom, "document.getElementById('fullBtn').click(); true");
  check("and back", await waitFor(() => sam.top("!document.getElementById('shell').classList.contains('focus')")));

  // leaving from the rail: the room says goodbye to the party, and the app takes it off the rail
  await sam.top("document.querySelector('#serverList .srv').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 40, clientY: 120 })); true");
  await sam.top("[...document.querySelectorAll('#railMenu button')].find(function (b) { return b.textContent === 'Leave this party'; }).click(); true");
  check("Leave this party leaves and takes the server off the rail", await waitFor(() => sam.top(
    "document.querySelectorAll('#serverList .srv-item').length === 0 && document.getElementById('view-home').classList.contains('current')")));
  check("the host sees Sam go", await waitFor(() => host.frame(hostRoom, "document.getElementById('count').textContent.startsWith('1 person')")));

  // About and How over the window; a refused certificate shown in the server's place
  await host.top(`__events.push({ type: "info", page: "how", title: "How PrivatePartyNet works" }); true`);
  check("How opens over the window", await waitFor(() => host.top("!document.getElementById('overlay').hidden && document.getElementById('overlayFrame').src.includes('how.html')")));
  check("and uses the window's bridge", await waitFor(() => host.frame("how.html", "window.pywebview === window.top.pywebview")));
  await host.shot("shell-how");
  await host.top("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); true");
  check("Escape closes it", await waitFor(() => host.top("document.getElementById('overlay').hidden")));
  await host.top(`__events.push({ type: "refused", origin: "http://127.0.0.1:${local}" }); true`);
  check("a refused certificate is shown in that server's place, not over the window", await waitFor(() => host.top(
    "!!document.querySelector('.view-note.current') && document.querySelector('.view-note.current').textContent.includes('This party was stopped') && !!document.getElementById('serverList').children.length")));
  await host.shot("shell-refused");

  console.log(failed ? `RESULT FAIL ${failed}` : "RESULT PASS");
  killAll();
  process.exit(failed ? 1 : 0);
}

main().catch((err) => { console.log("FAIL " + (err && err.stack || err)); killAll(); process.exit(1); });
