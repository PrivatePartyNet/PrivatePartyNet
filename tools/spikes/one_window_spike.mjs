// Spike: can one app window hold several servers' rooms as frames? Headless Edge, two real parties, a stand-in shell page.
// Usage: node tools/spikes/one_window_spike.mjs <project root> <python>. Passed 17 of 17 on 2026-11-04. Bounded: a watchdog kills all at RUN_LIMIT_MS.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const ROOT = process.argv[2];
const PYTHON = process.argv[3];
const HERE = import.meta.dirname;
const EDGE = ["C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "C:/Program Files/Microsoft/Edge/Application/msedge.exe"].find(existsSync);
const PORT_A = 8797, PORT_B = 8796, SHELL_PORT = 8781, DEBUG_PORT = 9334, RUN_LIMIT_MS = 300_000;
const children = [];
let failed = 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function check(name, ok, detail = "") { console.log(`${ok ? "ok   " : "FAIL "}${name}${detail ? ` (${detail})` : ""}`); if (!ok) failed += 1; }
async function waitFor(test, ms = 30_000) {
  const end = Date.now() + ms;
  while (Date.now() < end) { try { if (await test()) return true; } catch { /* still loading */ } await sleep(400); }
  return false;
}
function killAll() { for (const c of children) if (!c.killed) c.kill(); }
setTimeout(() => { console.log("FAIL run limit"); killAll(); process.exit(1); }, RUN_LIMIT_MS).unref();
function start(cmd, args) { const c = spawn(cmd, args, { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] }); children.push(c); return c; }

async function party(port) {
  const child = start(PYTHON, [join(ROOT, "tests", "browser", "serve_for_e2e.py"), String(port), "315902", "280"]);
  const info = { tokens: {} };
  await new Promise((done) => {
    let text = "";
    const timer = setTimeout(done, 20_000);
    child.stdout.on("data", (chunk) => {
      text += chunk;
      for (const [, who, token] of text.matchAll(/JOIN (\w+) (\S+)/g)) info.tokens[who] = token;
      const m = text.match(/READY (\d+)/);
      if (m && Object.keys(info.tokens).length === 2) { info.local = Number(m[1]); clearTimeout(timer); done(); }
    });
  });
  return info;
}

// One DevTools connection to the browser; every page and frame is a flattened session on it.
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
    await this.b.send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: false, flatten: true }, this.s);
    await this.b.send("Page.navigate", { url }, this.s);
  }
  async evalTop(expr) { return this.value(await this.b.send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }, this.s)); }
  value(r) { if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "eval"); return r.result.value; }
  // Evaluate in the room frame whose address contains `match`: a frame in its own process (another site), or one in the page's.
  async evalFrame(match, expr) {
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
}

const shellUrl = (frames, extra = "") => `http://127.0.0.1:${SHELL_PORT}/?frames=${encodeURIComponent(frames.join("|"))}${extra}`;

async function main() {
  const [A, B] = await Promise.all([party(PORT_A), party(PORT_B)]);
  check("two parties are up", Boolean(A.local && B.local));
  const shell = start(PYTHON, [join(HERE, "ppn_spike_shellserver.py"), String(SHELL_PORT), "280"]);
  await new Promise((done) => { shell.stdout.on("data", (c) => { if (String(c).includes("SHELL READY")) done(); }); setTimeout(done, 10_000); });

  const profile = mkdtempSync(join(tmpdir(), "ppn-spike-edge-"));
  start(EDGE, ["--headless=new", "--mute-audio", "--no-sandbox", "--disable-gpu", "--no-first-run", `--user-data-dir=${profile}`,
    `--remote-debugging-port=${DEBUG_PORT}`, "--ignore-certificate-errors", "--allow-loopback-in-peer-connection",
    "--disable-features=WebRtcHideLocalIpsWithMdns", "--autoplay-policy=no-user-gesture-required",
    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "about:blank"]);
  check("browser up", await waitFor(async () => (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/version`)).ok));
  const version = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/version`)).json();
  const browser = new Browser(version.webSocketDebuggerUrl);
  await browser.open();

  // The host of party A: its own room (plain HTTP on 127.0.0.1) as a frame of the shell (also 127.0.0.1, another port).
  const host = new Person(browser);
  await host.open(shellUrl([`http://127.0.0.1:${A.local}/host`]));
  const hostRoom = `127.0.0.1:${A.local}`;
  check("the host's own room loads in a frame", await waitFor(() => host.evalFrame(hostRoom, "!!document.getElementById('goLive')")));

  // Sam: party A's room (HTTPS, another site than the shell) and party B's room, both frames of one shell.
  const sam = new Person(browser);
  await sam.open(shellUrl([`https://127.0.0.1:${PORT_A}/join?token=${A.tokens.Sam}`, `https://127.0.0.1:${PORT_B}/join?token=${B.tokens.Sam}`]));
  const samA = `127.0.0.1:${PORT_A}`, samB = `127.0.0.1:${PORT_B}`;
  const signedIn = await waitFor(() => sam.evalFrame(samA, "!!document.getElementById('partyName') && document.getElementById('count').textContent.startsWith('2 people')"));
  const diag = await sam.evalFrame(samA, "location.href + ' | ' + document.title + ' | count=' + (document.getElementById('count') || {}).textContent + ' | status=' + (document.getElementById('status') || {}).textContent + ' | body=' + document.body.innerText.slice(0, 200).replace(/\\s+/g, ' ')").catch((e) => "unreadable: " + e.message);
  check("an HTTPS room signs in inside a frame (its cookie reaches the frame)", signedIn, diag);
  if (!signedIn) {
    const cookies = await browser.send("Network.getCookies", { urls: [`https://${samA}/`] }, sam.s).catch((e) => ({ error: e.message }));
    console.log("   cookies for A in Sam's context:", JSON.stringify(cookies).slice(0, 400));
  }
  check("a second server's room loads in the same window", await waitFor(() => sam.evalFrame(samB, "!!document.getElementById('partyName')")));
  check("the host sees Sam arrive (event stream through the frame)", await waitFor(() => host.evalFrame(hostRoom, "document.getElementById('count').textContent.startsWith('2 people')")));

  // Chat from a frame
  await sam.evalFrame(samA, "document.getElementById('chatText').value = 'hello from a frame'; document.getElementById('chatForm').requestSubmit(); true");
  check("chat sent from a frame reaches the host's frame", await waitFor(() => host.evalFrame(hostRoom, "document.getElementById('messages').textContent.includes('hello from a frame')")));

  // A room tells the shell something
  await sam.evalFrame(samA, "parent.postMessage({ ppn: 'status', unread: 2 }, '*'); true");
  const msgs = await waitFor(async () => (await sam.evalTop("window.__msgs.length")) > 0) ? await sam.evalTop("window.__msgs") : [];
  check("a room frame's message reaches the shell, which knows which frame sent it", msgs.some((m) => m.origin === `https://${samA}` && m.frame === "0" && m.data.unread === 2), JSON.stringify(msgs).slice(0, 160));

  // Voice in frames: the microphone works under allow="microphone"
  const joinVoice = (p, room) => p.evalFrame(room, "[...document.querySelectorAll('#channelList .channel.voice .channel-row button')].find(function (b) { return b.textContent === 'Join'; }).click(); true");
  await joinVoice(host, hostRoom);
  await joinVoice(sam, samA);
  const hearing = (p, room) => p.evalFrame(room, "(function () { var a = document.querySelectorAll('audio'); return a.length === 1 && a[0].srcObject.getAudioTracks()[0].muted === false && a[0].srcObject.getAudioTracks()[0].readyState === 'live'; })()");
  check("voice works between frames: the host hears Sam", await waitFor(() => hearing(host, hostRoom)));
  check("and Sam hears the host", await waitFor(() => hearing(sam, samA)));

  // Sam looks at server B; server A's frame is hidden but stays loaded
  await sam.evalTop("show(1); true");
  await sleep(4000);
  check("with A's frame hidden, Sam still hears the host", await hearing(sam, samA));
  check("and the host still hears Sam", await hearing(host, hostRoom));
  check("Sam is still in A's voice channel", await sam.evalFrame(samA, "!document.getElementById('voiceBar').hidden"));
  await sam.evalTop("show(0); true");

  // A screen between frames: the host goes live from its own screen through the party's frame feed
  await host.evalFrame(hostRoom, "document.getElementById('goLive').click(); true");
  check("the host's screen reaches Sam's frame as a tile", await waitFor(() => sam.evalFrame(samA, "[...document.querySelectorAll('#tiles .label')].some(function (l) { return l.textContent.startsWith('Host - '); })")));
  check("and its picture plays", await waitFor(() => sam.evalFrame(samA, "[...document.querySelectorAll('#tiles video')].some(function (v) { return v.videoWidth > 0 && !v.paused; })"), 30_000));

  // Control: the same room without allow="microphone" is refused the microphone
  const control = new Person(browser);
  await control.open(shellUrl([`https://127.0.0.1:${PORT_B}/join?token=${B.tokens.Alex}`], "&noallow=1"));
  await waitFor(() => control.evalFrame(samB, "!!document.getElementById('partyName')"));
  const refused = await control.evalFrame(samB, "navigator.mediaDevices.getUserMedia({ audio: true }).then(function () { return 'allowed'; }, function (e) { return e.name; })").catch((e) => "error " + e.message);
  check("without the allow attribute the frame is refused the microphone", refused === "NotAllowedError", refused);
  const allowed = await sam.evalFrame(samB, "navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) { s.getTracks().forEach(function (t) { t.stop(); }); return 'allowed'; }, function (e) { return e.name; })").catch((e) => "error " + e.message);
  check("with it, the same room is allowed", allowed === "allowed", allowed);

  console.log(failed ? `RESULT FAIL ${failed}` : "RESULT PASS");
  killAll();
  process.exit(failed ? 1 : 0);
}
main().catch((e) => { console.log("FAIL crashed: " + (e && e.stack || e)); killAll(); process.exit(1); });
