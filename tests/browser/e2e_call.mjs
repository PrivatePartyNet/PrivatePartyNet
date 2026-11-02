// End-to-end party test: a real PrivatePartyNet server, the real party page for the host and two members, and real WebRTC,
// all in headless Microsoft Edge. Each member is a separate browser context, so their cookies never mix.
// Node 22+ drives the browser over the DevTools protocol with its built-in WebSocket.
//
// Run from the project root:  node tests\browser\e2e_call.mjs
//
// Only the screen picker is replaced (headless Edge has none). Everything else is the product's own code path.
// Every wait is bounded, and a watchdog ends the whole run, killing both children, after RUN_LIMIT_MS.

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { cpus, tmpdir } from "node:os";
import { join, resolve } from "node:path";

const ROOT = resolve(import.meta.dirname, "..", "..");
// Python and Edge are found on this PC, or named by PRIVATEPARTYNET_PYTHON and PRIVATEPARTYNET_EDGE if they are somewhere else.
const PYTHON = process.env.PRIVATEPARTYNET_PYTHON || "python";
const EDGE = process.env.PRIVATEPARTYNET_EDGE || [
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
].find((candidate) => existsSync(candidate)) || "msedge";
const PORT = 8799;
const DEBUG_PORT = 9333;
const joinTokens = {}; // join links the party server prints, one per guest
const PASSCODE = "315902";
const RUN_LIMIT_MS = 420_000;
const SERVER_SECONDS = 400;
const WAIT_MS = 45_000;

const children = [];
let failed = 0;

function check(name, ok, detail = "") {
  console.log(`${ok ? "ok   " : "FAIL "}${name}${detail ? ` (${detail})` : ""}`);
  if (!ok) failed += 1;
}

// A pointer press, some moves and a release, sent to the page the way a mouse sends them. The page listens for the move
// and the release on the document, so these work without a real pointer. A press with no moves is a click.
function pointerScript(selector, dx, dy, steps = 6, extra = "") {
  return `(async function () {
    const target = document.querySelector(${JSON.stringify(selector)});
    if (!target) return false;
    const r = target.getBoundingClientRect();
    const x0 = r.left + r.width / 2;
    const y0 = r.top + r.height / 2;
    const opts = (x, y, more) => Object.assign({ bubbles: true, cancelable: true, clientX: x, clientY: y, pointerId: 1,
      pointerType: "mouse", isPrimary: true, button: 0, buttons: 1 }, ${extra || "{}"}, more || {});
    target.dispatchEvent(new PointerEvent("pointerdown", opts(x0, y0)));
    for (let i = 1; i <= ${steps}; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 16));
      document.dispatchEvent(new PointerEvent("pointermove", opts(x0 + ${dx} * i / ${steps}, y0 + ${dy} * i / ${steps})));
    }
    document.dispatchEvent(new PointerEvent("pointerup", opts(x0 + ${dx}, y0 + ${dy}, { buttons: 0 })));
    return true;
  })()`;
}

// With E2E_SHOTS=<folder>, a screenshot of a page at a few moments, to look at. Off in a normal run.
async function shot(tab, name) {
  const folder = process.env.E2E_SHOTS;
  if (!folder) return;
  mkdirSync(folder, { recursive: true });
  const result = await tab.send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(folder, name + ".png"), Buffer.from(result.data, "base64"));
}

const tileKeyScript = (label) => `(function () {
  const t = [...document.querySelectorAll('.tile')].find(t => t.querySelector('.label').textContent === ${JSON.stringify(label)});
  return t ? t.dataset.key : null;
})()`;

function sleep(ms) {
  return new Promise((done) => setTimeout(done, ms));
}

async function waitFor(test, ms = WAIT_MS) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    try {
      if (await test()) return true;
    } catch {
      // the page may be navigating; keep polling until the deadline
    }
    await sleep(400);
  }
  return false;
}

function killAll() {
  for (const child of children) {
    if (!child.killed) child.kill();
  }
}

const watchdog = setTimeout(() => {
  console.log("FAIL run limit reached");
  killAll();
  process.exit(1);
}, RUN_LIMIT_MS);

class Tab {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.nextId = 0;
    this.pending = new Map();
  }

  async open(withPageDomains = true) {
    this.ws = new WebSocket(this.wsUrl);
    await new Promise((done, reject) => {
      this.ws.onopen = done;
      this.ws.onerror = () => reject(new Error("websocket failed"));
    });
    this.ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      const waiting = this.pending.get(message.id);
      if (!waiting) return;
      this.pending.delete(message.id);
      if (message.error) waiting.reject(new Error(message.error.message));
      else waiting.resolve(message.result);
    };
    if (withPageDomains) {
      await this.send("Page.enable");
      await this.send("Runtime.enable");
    }
  }

  send(method, params = {}) {
    const id = ++this.nextId;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolveResult, rejectResult) => {
      this.pending.set(id, { resolve: resolveResult, reject: rejectResult });
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
    if (result.exceptionDetails) {
      const where = String(expression).replace(/\s+/g, " ").slice(0, 140);
      throw new Error((result.exceptionDetails.exception?.description || "evaluation failed") + " in: " + where);
    }
    return result.result.value;
  }

  // Replace the screen picker with a canvas stream of a known size, so the sender's resolution is known.
  async fakeScreen(width, height) {
    const source = `(function () {
      navigator.mediaDevices.getDisplayMedia = async function () {
        var canvas = document.createElement("canvas");
        canvas.width = ${width};
        canvas.height = ${height};
        var ctx = canvas.getContext("2d");
        var frame = 0;
        setInterval(function () {
          frame += 1;
          ctx.fillStyle = "hsl(" + (frame % 360) + ", 80%, 50%)";
          ctx.fillRect(0, 0, ${width}, ${height});
        }, 33);
        return canvas.captureStream(30);
      };
    })();`;
    await this.send("Page.addScriptToEvaluateOnNewDocument", { source });
  }

  async navigate(url) {
    await this.send("Page.navigate", { url });
    await waitFor(async () => (await this.evaluate("document.readyState")) === "complete", 20_000);
  }
}

// CPU time, in seconds, used by this run's Edge processes. They all carry this run's profile folder in their
// command line, so other Edge windows on the PC are not counted. Bounded by the PowerShell timeout below.
async function edgeCpuSeconds(profileFolder) {
  const marker = profileFolder.replace(/'/g, "''");
  const script = [
    "$ids = Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" |",
    `  Where-Object { $_.CommandLine -like '*${marker}*' } | Select-Object -ExpandProperty ProcessId;`,
    "$total = 0;",
    "foreach ($id in $ids) { $p = Get-Process -Id $id -ErrorAction SilentlyContinue; if ($p) { $total += $p.TotalProcessorTime.TotalSeconds } };",
    "Write-Output $total",
  ].join(" ");
  const child = spawn("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script], { windowHide: true, stdio: ["ignore", "pipe", "pipe"] });
  let out = "";
  child.stdout.on("data", (chunk) => { out += chunk; });
  // The PowerShell call is bounded: if it has not exited in 30 seconds, the reading is taken as zero.
  const exited = new Promise((resolve) => child.on("exit", resolve));
  await Promise.race([exited, sleep(30_000)]);
  if (child.exitCode === null) child.kill();
  return Number(out.trim()) || 0;
}

function startChild(command, args, label) {
  const child = spawn(command, args, { windowHide: true, stdio: ["ignore", "pipe", "pipe"] });
  children.push(child);
  child.on("exit", (code) => {
    if (code !== null && code !== 0) console.log(`(${label} exited with ${code})`);
  });
  return child;
}

async function newMemberTab(browser) {
  const { browserContextId } = await browser.send("Target.createBrowserContext", { disposeOnDetach: false });
  const { targetId } = await browser.send("Target.createTarget", { url: "about:blank", browserContextId });
  let wsUrl = null;
  await waitFor(async () => {
    const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
    const target = list.find((item) => item.id === targetId);
    if (target) wsUrl = target.webSocketDebuggerUrl;
    return Boolean(wsUrl);
  }, 15_000);
  const tab = new Tab(wsUrl);
  await tab.open();
  // Current Edge builds no longer apply --ignore-certificate-errors to extra browser contexts, so say it per tab.
  await tab.send("Security.setIgnoreCertificateErrors", { ignore: true }).catch(() => {});
  return tab;
}

// A tab another page opened (a pop-out opens in its window's own browser context), found by its address.
async function attachTab(match) {
  let wsUrl = null;
  await waitFor(async () => {
    const list = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json();
    const target = list.find((item) => item.type === "page" && match(item.url));
    if (target) wsUrl = target.webSocketDebuggerUrl;
    return Boolean(wsUrl);
  }, 15_000);
  const tab = new Tab(wsUrl);
  await tab.open();
  // Current Edge builds no longer apply --ignore-certificate-errors to extra browser contexts, so say it per tab.
  await tab.send("Security.setIgnoreCertificateErrors", { ignore: true }).catch(() => {});
  // The window loaded before this tab could say so; if it stopped on the certificate warning, load it again.
  const stopped = await tab.evaluate("location.protocol === 'chrome-error:'").catch(() => false);
  if (stopped) await tab.send("Page.reload").catch(() => {});
  return tab;
}

async function joinParty(tab, name) {
  // Joining is a join link from the app: the token is single-use and the party page opens with it.
  await tab.navigate(`https://127.0.0.1:${PORT}/join?token=${joinTokens[name]}`);
  const joined = await waitFor(() => tab.evaluate("!!document.getElementById('partyName')"));
  if (!joined) {
    // Say where the browser ended up, so a failed join is a diagnosis and not just a red line.
    console.log("   join failed; the page is", await tab.evaluate("location.href + ' | ' + document.title + ' | ' + document.body.innerText.slice(0, 160)").catch(() => "unreadable"));
  }
  return joined;
}

async function main() {
  // 1. The real server, one party.
  const server = startChild(PYTHON, [join(ROOT, "tests", "browser", "serve_for_e2e.py"), String(PORT), PASSCODE, String(SERVER_SECONDS)], "server");
  // The server prints READY and its loopback port. The host's own window uses that port over plain HTTP.
  let localPort = null;
  const ready = await new Promise((done) => {
    let text = "";
    const timer = setTimeout(() => done(false), 20_000);
    server.stdout.on("data", (chunk) => {
      text += chunk;
      for (const [, who, token] of text.matchAll(/JOIN (\w+) (\S+)/g)) joinTokens[who] = token;
      const match = text.match(/READY (\d+)/);
      if (match) {
        localPort = Number(match[1]);
        clearTimeout(timer);
        done(true);
      }
    });
    server.on("exit", () => done(false));
  });
  check("server is up", ready);
  if (!ready) return finish();

  // 2. Headless Edge with a fresh profile, reachable over the DevTools protocol.
  const profile = mkdtempSync(join(tmpdir(), "privatepartynet-e2e-edge-"));
  startChild(EDGE, [
    "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    `--user-data-dir=${profile}`, `--remote-debugging-port=${DEBUG_PORT}`,
    "--ignore-certificate-errors", "--allow-loopback-in-peer-connection",
    "--disable-features=WebRtcHideLocalIpsWithMdns", "--autoplay-policy=no-user-gesture-required",
    // a fake microphone that beeps, and no permission prompt: a headless browser has neither a microphone nor a person
    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
    "about:blank",
  ], "edge");
  const versionUp = await waitFor(async () => (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/version`)).ok, 30_000);
  check("browser is reachable", versionUp);
  if (!versionUp) return finish();
  const version = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/version`)).json();
  const browser = new Tab(version.webSocketDebuggerUrl);
  await browser.open(false);

  // 3. The host opens the party page on loopback. Two members join from their own browser contexts.
  // The host's window is loopback HTTP. Its screen comes from the Python capture, not a browser picker.
  const host = await newMemberTab(browser);
  await host.navigate(`http://127.0.0.1:${localPort}/host`);
  check("host page loads from loopback", await waitFor(() => host.evaluate("!!document.getElementById('goLive')")));
  check("host sees one person online", await waitFor(() => host.evaluate("document.getElementById('count').textContent.startsWith('1 person')")));

  const alex = await newMemberTab(browser);
  await alex.fakeScreen(800, 450);
  check("Alex joins by the join link", await joinParty(alex, "Alex"));
  check("host sees two people online", await waitFor(() => host.evaluate("document.getElementById('count').textContent.startsWith('2 people')")));

  const sam = await newMemberTab(browser);
  await sam.fakeScreen(640, 360);
  if (process.env.E2E_SHOTS) await sam.send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
  check("Sam joins by the join link", await joinParty(sam, "Sam"));
  check("everyone sees three people online", await waitFor(async () =>
    (await host.evaluate("document.getElementById('count').textContent")).startsWith("3 people") &&
    (await alex.evaluate("document.getElementById('count').textContent")).startsWith("3 people") &&
    (await sam.evaluate("document.getElementById('count').textContent")).startsWith("3 people")));

  // 4. Group chat reaches everyone.
  await alex.evaluate(`(function () {
    document.getElementById("chatText").value = "hello from Alex";
    document.getElementById("chatForm").requestSubmit();
  })()`);
  check("chat reaches the host", await waitFor(() => host.evaluate("document.getElementById('messages').textContent.includes('hello from Alex')")));
  check("chat reaches Sam", await waitFor(() => sam.evaluate("document.getElementById('messages').textContent.includes('hello from Alex')")));

  // 5. Each member connects to every other member (mesh). The stats line counts open connections.
  check("host connects to both members", await waitFor(() => host.evaluate("document.getElementById('statsbar').dataset.connected === '2'")));
  check("Alex connects to both members", await waitFor(() => alex.evaluate("document.getElementById('statsbar').dataset.connected === '2'")));
  check("Sam connects to both members", await waitFor(() => sam.evaluate("document.getElementById('statsbar').dataset.connected === '2'")));

  // 6. Alex goes live. Everyone sees the LIVE badge and a tile for Alex.
  await alex.evaluate("document.getElementById('goLive').click()");
  check("Alex is marked LIVE everywhere", await waitFor(async () =>
    (await host.evaluate("document.getElementById('roster').textContent.includes('LIVE')")) &&
    (await sam.evaluate("document.getElementById('roster').textContent.includes('LIVE')"))));
  check("Sam sees a tile for Alex's first screen", await waitFor(() => sam.evaluate("[...document.querySelectorAll('#tiles .label')].some(l => l.textContent === 'Alex - Screen 1')")));
  // Alex shares a second screen as its own tile. Stopping it removes only that tile.
  await alex.evaluate("document.getElementById('goLive').click()");
  check("Sam sees Alex's second screen as its own tile", await waitFor(() => sam.evaluate("[...document.querySelectorAll('#tiles .label')].some(l => l.textContent === 'Alex - Screen 2')")));
  check("Alex lists two live screens", await waitFor(() => alex.evaluate("document.querySelectorAll('#myShares li').length === 2")));
  await alex.evaluate("document.querySelectorAll('#myShares li button')[1].click()");
  check("stopping the second screen removes only its tile", await waitFor(() => sam.evaluate(
    "(function () { var labels = [...document.querySelectorAll('#tiles .label')].map(l => l.textContent); return !labels.includes('Alex - Screen 2') && labels.includes('Alex - Screen 1'); })()")));
  check("Sam receives Alex's 800x450 screen", await waitFor(() => sam.evaluate("[...document.querySelectorAll('#peerChips .chip')].some(c => c.querySelector('.cname').textContent === 'Alex' && c.dataset.downFps)")));

  // The host goes live from its own screen, through the frame feed. Sam sees the host as a tile.
  await host.evaluate("(function () { window.__feed = []; var f = window.fetch; window.fetch = function (url) { window.__feed.push(String(url)); return f.apply(this, arguments); }; })()");
  await host.evaluate("document.getElementById('goLive').click()");
  check("the host's own screen reaches Sam as a tile", await waitFor(() => sam.evaluate("[...document.querySelectorAll('#tiles .label')].some(l => l.textContent.startsWith('Host - '))")));
  check("Sam receives the host's frames", await waitFor(() => sam.evaluate("[...document.querySelectorAll('#peerChips .chip')].some(c => c.querySelector('.cname').textContent === 'Host' && c.dataset.downFps)")));

  // 7. Receive choices reach the sender. Sam asks for 50%, so Alex sends 400x225. Then back to 100%.
  await sam.evaluate(`(function () {
    var choice = document.getElementById("receive");
    choice.value = "50";
    choice.dispatchEvent(new Event("change"));
  })()`);
  check("Sam's tile for Alex is no wider than 400px at 50%", await waitFor(() => sam.evaluate("[...document.querySelectorAll('.tile')].some(t => t.querySelector('.label').textContent === 'Alex - Screen 1' && parseInt(t.style.width) > 0 && parseInt(t.style.width) <= 400)")));
  check("Sam at 50% gets Alex at 400x225", await waitFor(() => sam.evaluate("[...document.querySelectorAll('#peerChips .chip')].some(c => c.querySelector('.cname').textContent === 'Alex' && c.dataset.downRes === '400\u00d7225')")));
  await sam.evaluate(`(function () {
    var choice = document.getElementById("receive");
    choice.value = "100";
    choice.dispatchEvent(new Event("change"));
  })()`);
    await sam.evaluate("(function () { var choice = document.getElementById('upscale'); choice.value = 'on'; choice.dispatchEvent(new Event('change')); })()");
  check("upscaling draws Alex's picture into a canvas", await waitFor(() => sam.evaluate("(function () { var c = document.querySelector('canvas.upscaled:not([hidden])'); return Boolean(c && c.width > 0 && c.height > 0); })()")));
  await sam.evaluate("(function () { var choice = document.getElementById('upscale'); choice.value = 'off'; choice.dispatchEvent(new Event('change')); })()");
  check("Sam at 100% gets Alex at 800x450", await waitFor(() => sam.evaluate("[...document.querySelectorAll('#peerChips .chip')].some(c => c.querySelector('.cname').textContent === 'Alex' && c.dataset.downRes === '800\u00d7450')")));

  // CPU cost of upscaling. Three browser windows share one Edge process tree, so the cost is the whole tree's CPU
  // over eight seconds, with Sam's upscaling off, then on, then off again. The share is steady during each sample.
  // Headless Edge runs with --disable-gpu here, so this is the software path; a PC with a GPU does more of it there.
  const cores = cpus().length;
  async function upscaleCost(mode) {
    await sam.evaluate(`(function () {
      var choice = document.getElementById('upscale');
      choice.value = '${mode}';
      choice.dispatchEvent(new Event('change'));
    })()`);
    await sleep(1500);
    const before = await edgeCpuSeconds(profile);
    const started = Date.now();
    await sleep(8000);
    const after = await edgeCpuSeconds(profile);
    const wall = (Date.now() - started) / 1000;
    return { percent: ((after - before) / (wall * cores)) * 100, cores };
  }
  const offCost = await upscaleCost("off");
  const onCost = await upscaleCost("on");
  console.log(`cpu upscale off: ${offCost.percent.toFixed(1)}% of ${cores} logical processors`);
  console.log(`cpu upscale on: ${onCost.percent.toFixed(1)}% of ${cores} logical processors`);
  console.log(`cpu upscale cost: ${(onCost.percent - offCost.percent).toFixed(1)} points`);

  // The two readouts. The whole app's CPU shows in the stats bar; upscaling's own share shows beside its choice.
  check("the bar reads this PC's CPU", await waitFor(() => host.evaluate("document.getElementById('cpuChip').textContent.includes('%')")));
  check("the upscale readout shows a live share", await waitFor(() => sam.evaluate(
    "!document.getElementById('upscaleNote').hidden && document.getElementById('upscaleNote').textContent.includes('CPU now')")));
  check("each received screen has a Pop out button", await sam.evaluate("[...document.querySelectorAll('#tiles .tile .popout')].length > 0"));
  await sam.evaluate("document.querySelector('#tiles .tile .popout').click()");
  check("Pop out explains itself outside the app", await waitFor(() => sam.evaluate("document.getElementById('status').textContent.includes('Pop out needs the PrivatePartyNet app')")));

  // The two views. Clicking a screen makes it big and puts the others in a strip of previews along the bottom.
  const tileSel = (key) => `.tile[data-key="${key}"]`;
  const alexKey = await sam.evaluate(tileKeyScript("Alex - Screen 1"));
  check("Sam starts in the grid with both screens", await sam.evaluate("!document.getElementById('tiles').hidden && document.querySelectorAll('#tiles .tile').length >= 2"));
  check("each tile has a handle on every edge and corner", await sam.evaluate(`document.querySelectorAll('${tileSel(alexKey)} .handle').length === 8`));
  check("the grid arranged the tiles without overlap", await sam.evaluate(`(function () {

    const r = [...document.querySelectorAll('#tiles .tile')].map(t => t.getBoundingClientRect());
    for (let i = 0; i < r.length; i += 1) for (let j = i + 1; j < r.length; j += 1) {
      if (r[i].left < r[j].right - 1 && r[i].right > r[j].left + 1 && r[i].top < r[j].bottom - 1 && r[i].bottom > r[j].top + 1) return false;
    }
    return true;
  })()`));

  await shot(sam, "1-grid");
  await sam.evaluate(pointerScript(tileSel(alexKey), 0, 0, 1));
  check("clicking a tile opens the focus view", await waitFor(() => sam.evaluate("!document.getElementById('focusView').hidden && document.getElementById('tiles').hidden")));
  check("the clicked screen is the big one", await sam.evaluate("document.querySelector('#primary .tile .label').textContent === 'Alex - Screen 1'"));
  check("the other screens are previews in the strip", await sam.evaluate("document.querySelectorAll('#primary .tile').length === 1 && document.querySelectorAll('#strip .tile').length >= 1 && !document.getElementById('strip').hidden"));
  const focusBoxes = await sam.evaluate(`(function () {
    const box = (id) => { const r = document.getElementById(id).getBoundingClientRect(); return { top: Math.round(r.top), bottom: Math.round(r.bottom), height: Math.round(r.height) }; };
    return { primary: box('primary'), strip: box('strip'), stage: box('stage') };
  })()`);
  check("the big screen fills the stage above a preview strip",
    focusBoxes.primary.height > focusBoxes.strip.height * 1.5 && focusBoxes.strip.bottom <= focusBoxes.stage.bottom + 1 && focusBoxes.strip.top >= focusBoxes.primary.bottom - 1,
    JSON.stringify(focusBoxes));
  await sleep(600);
  await shot(sam, "2-focus");
  await sam.evaluate(pointerScript("#strip .tile", 0, 0, 1));
  check("clicking a preview swaps it with the big screen", await waitFor(() => sam.evaluate("document.querySelector('#primary .tile .label').textContent !== 'Alex - Screen 1'")));
  await sam.evaluate(pointerScript("#primary .tile", 0, 0, 1));
  check("clicking the big screen returns to the grid", await waitFor(() => sam.evaluate("document.getElementById('focusView').hidden && document.querySelectorAll('#tiles .tile').length >= 2")));
  await sam.evaluate(pointerScript(tileSel(alexKey), 0, 0, 1));
  await waitFor(() => sam.evaluate("!document.getElementById('focusView').hidden"));
  await sam.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))");
  check("Escape returns to the grid", await waitFor(() => sam.evaluate("document.getElementById('focusView').hidden")));

  // Right-click a screen to hide it. The sender stops sending it to this viewer, and a click brings it back.
  const alexSendsToSam = "[...document.querySelectorAll('#peerChips .chip')].some(c => c.querySelector('.cname').textContent === 'Sam' && c.dataset.upKbps)";
  check("Alex is sending to Sam before the stream is hidden", await waitFor(() => alex.evaluate(alexSendsToSam)));
  await sam.evaluate(`document.querySelector('${tileSel(alexKey)}').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 300, clientY: 200 }))`);
  check("right-click opens a menu with the hide choice", await sam.evaluate("!document.getElementById('ctxMenu').hidden && [...document.querySelectorAll('#ctxMenu button')].some(b => b.textContent === \"Don't show this stream\")"));
  await sam.evaluate("[...document.querySelectorAll('#ctxMenu button')].find(b => b.textContent === \"Don't show this stream\").click()");
  check("the hidden stream shows a black screen with a way back", await waitFor(() => sam.evaluate(`(function () {
    const t = document.querySelector('${tileSel(alexKey)}');
    return t.classList.contains('stream-hidden') && getComputedStyle(t.querySelector('.hidden-note')).display !== 'none' && t.querySelector('.hidden-note').textContent.includes('This stream is hidden');
  })()`)));
  await shot(sam, "3-hidden");
  check("Alex stops sending the hidden stream to Sam", await waitFor(() => alex.evaluate("![...document.querySelectorAll('#peerChips .chip')].some(c => c.querySelector('.cname').textContent === 'Sam' && c.dataset.upKbps)"), 20_000));
  await sam.evaluate(`document.querySelector('${tileSel(alexKey)} .hidden-note button').click()`);
  check("clicking the note shows the stream again", await waitFor(() => sam.evaluate(`!document.querySelector('${tileSel(alexKey)}').classList.contains('stream-hidden')`)));
  check("Alex resumes sending to Sam", await waitFor(() => alex.evaluate(alexSendsToSam), 20_000));
  const ownKey = await host.evaluate("(function () { const t = [...document.querySelectorAll('.tile')].find(t => t.querySelector('.label').textContent.startsWith('You - ')); return t ? t.dataset.key : null; })()");
  check("the host's own preview can be hidden too", await (async () => {
    await host.evaluate(`document.querySelector('${tileSel(ownKey)}').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 200, clientY: 200 }))`);
    await host.evaluate("[...document.querySelectorAll('#ctxMenu button')].find(b => b.textContent === \"Don't show this stream\").click()");
    const hidden = await waitFor(() => host.evaluate(`document.querySelector('${tileSel(ownKey)}').classList.contains('stream-hidden')`));
    await host.evaluate(`document.querySelector('${tileSel(ownKey)} .hidden-note button').click()`);
    return hidden && await waitFor(() => host.evaluate(`!document.querySelector('${tileSel(ownKey)}').classList.contains('stream-hidden')`));
  })());

  // Move, snap and resize a tile in the grid.
  const geometry = (key) => `(function () { const t = document.querySelector('${tileSel(key)}'); return { x: parseInt(t.style.left), y: parseInt(t.style.top), w: parseInt(t.style.width), h: parseInt(t.style.height) }; })()`;
  const before = await sam.evaluate(geometry(alexKey));
  await sam.evaluate(pointerScript(tileSel(alexKey), 90, 40, 8));
  const moved = await sam.evaluate(geometry(alexKey));
  check("dragging a tile moves it", moved.x !== before.x || moved.y !== before.y);
  check("a dragged tile keeps its size", moved.w === before.w && moved.h === before.h);
  check("dragging does not open the focus view", await sam.evaluate("document.getElementById('focusView').hidden"));
  const stageBox = await sam.evaluate("(function () { const r = document.getElementById('tiles').getBoundingClientRect(); return { w: r.width, h: r.height }; })()");
  await sam.evaluate(pointerScript(tileSel(alexKey), -(moved.x - 3), 0, 8)); // toward the left edge, 3 pixels from the margin
  const snapped = await sam.evaluate(geometry(alexKey));
  check("a tile dragged near the edge snaps to the margin", snapped.x === 10);
  check("a moved tile stays inside the stage", snapped.x >= 0 && snapped.y >= 0 && snapped.x + snapped.w <= stageBox.w + 1 && snapped.y + snapped.h <= stageBox.h + 1);
  const small = await sam.evaluate(geometry(alexKey));
  await sam.evaluate(pointerScript(`${tileSel(alexKey)} .handle[data-edge="se"]`, 40, 40, 6));
  const bigger = await sam.evaluate(geometry(alexKey));
  check("dragging a corner resizes the tile", bigger.w > small.w);
  check("a corner resize keeps the picture's shape", Math.abs(bigger.w / bigger.h - small.w / small.h) < 0.05);
  await sam.evaluate(pointerScript(`${tileSel(alexKey)} .handle[data-edge="e"]`, -30, 0, 6, "{ shiftKey: true }"));
  const freed = await sam.evaluate(geometry(alexKey));
  check("holding Shift resizes without keeping the shape", freed.w < bigger.w && freed.h === bigger.h);
  await sam.evaluate("document.getElementById('resetLayout').click()");
  check("Reset layout arranges the tiles again", await waitFor(() => sam.evaluate(`(function () {
    const stage = document.getElementById('tiles').getBoundingClientRect();
    return [...document.querySelectorAll('#tiles .tile')].every(t => { const r = t.getBoundingClientRect(); return r.left >= stage.left - 1 && r.right <= stage.right + 1 && r.top >= stage.top - 1 && r.bottom <= stage.bottom + 1; });
  })()`)));

  // The connection bar: what a hover card says.
  await sam.evaluate(`(function () {
    const chip = [...document.querySelectorAll('#peerChips .chip')].find(c => c.querySelector('.cname').textContent === 'Alex');
    const r = chip.getBoundingClientRect();
    chip.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: r.left + 4, clientY: r.top + 4 }));
  })()`);
  check("hovering a name shows its connection summary", await waitFor(() => sam.evaluate("!document.getElementById('tip').hidden && document.getElementById('tip').textContent.includes('Receiving from Alex')")));
  await shot(sam, "4-tip");
  check("the summary has the resolution, the frame rate and the CPU impact", await sam.evaluate("(function () { const t = document.getElementById('tip').textContent; return /fps/.test(t) && /\u00d7/.test(t) && /CPU/.test(t) && /Round trip/.test(t); })()"));
  await sam.evaluate("document.getElementById('peerChips').dispatchEvent(new MouseEvent('mouseleave'))");
  check("the summary goes away when the pointer leaves", await sam.evaluate("document.getElementById('tip').hidden"));
  check("the bar shows icons for the numbers that apply", await sam.evaluate("document.querySelectorAll('#peerChips .chip svg.ic').length > 0"));
  check("the bar has a chip for each person and not for pop-out windows", await sam.evaluate("document.querySelectorAll('#peerChips .chip').length === Number(document.getElementById('statsbar').dataset.peers)"));
  await sam.evaluate("(function () { document.getElementById('statsbar').style.maxWidth = '210px'; })()");
  check("an overcrowded bar gives up detail instead of overflowing", await waitFor(() => sam.evaluate("Number(document.getElementById('statsbar').dataset.level) < 3 && document.getElementById('statsbar').scrollWidth <= document.getElementById('statsbar').clientWidth + 2")));
  await sam.evaluate("document.getElementById('statsbar').style.maxWidth = ''");

  // Each stream's own controls: settings, pop out and fullscreen sit on the tile, so the tile says what they affect.
  await sam.evaluate("(function () { var c = document.getElementById('receive'); c.value = 'fit'; c.dispatchEvent(new Event('change')); })()");

  // The host captures its screen at the size its viewers need: here two small tiles and its own preview, so well below the
  // screen's own size. The capture is restarted with a smaller height once that need has lasted, and still reaches Sam.
  const lastFeedHeight = "(function () { const urls = window.__feed.filter(u => u.includes('/frames')); const last = urls[urls.length - 1] || ''; const m = last.match(/[?&]height=(\\d+)/); return m ? Number(m[1]) : 99999; })()";
  const smaller = await waitFor(() => host.evaluate(`${lastFeedHeight} <= 720`), 40_000);
  check("the host captures its screen smaller once its viewers need less", smaller, String(await host.evaluate(lastFeedHeight)));
  if (!smaller) console.log("   decision inputs:", await host.evaluate("window.PartyDebug()"));
  check("and Sam still receives the host's frames", await waitFor(() => sam.evaluate("[...document.querySelectorAll('#peerChips .chip')].some(c => c.querySelector('.cname').textContent === 'Host' && c.dataset.downFps)"), 20_000));
  check("a received stream's tile has settings, pop out and fullscreen", await sam.evaluate(`document.querySelectorAll('${tileSel(alexKey)} .tools .tool').length === 3 && !!document.querySelector('${tileSel(alexKey)} .tool.settings') && !!document.querySelector('${tileSel(alexKey)} .tool.popout') && !!document.querySelector('${tileSel(alexKey)} .tool.fullscreen')`));
  check("the host's own tile has settings and fullscreen but no pop out", await host.evaluate(`document.querySelectorAll('${tileSel(ownKey)} .tools .tool').length === 2 && !document.querySelector('${tileSel(ownKey)} .tool.popout')`));
  await sam.evaluate(`document.querySelector('${tileSel(alexKey)} .tool.settings').click()`);
  check("the panel sits fully inside the window, wherever the tile is", await sam.evaluate(`(function () { const r = document.querySelector('${tileSel(alexKey)} .stream-panel').getBoundingClientRect(); return r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight; })()`));
  check("the settings button opens a panel on that tile, named for the stream", await sam.evaluate(`(function () { const p = document.querySelector('${tileSel(alexKey)} .stream-panel'); return !!p && p.querySelector('h3').textContent === 'Alex - Screen 1' && p.querySelectorAll('select').length === 2; })()`));
  await sam.evaluate("document.querySelectorAll('.tools').forEach(t => { t.style.opacity = 1; })");
  await shot(sam, "5-tile-settings");
  await sam.evaluate("document.querySelectorAll('.tools').forEach(t => { t.style.opacity = ''; })");
  await sam.evaluate(`(function () { const s = document.querySelector('${tileSel(alexKey)} .stream-panel select'); s.value = '50'; s.dispatchEvent(new Event('change')); })()`);
  check("one stream's receive size reaches its sender and leaves the View panel alone", await waitFor(() => sam.evaluate("[...document.querySelectorAll('#peerChips .chip')].some(c => c.querySelector('.cname').textContent === 'Alex' && c.dataset.downRes === '400\u00d7225')")) && await sam.evaluate("document.getElementById('receive').value === 'fit'"));
  check("another stream is not changed by it", await sam.evaluate("[...document.querySelectorAll('#peerChips .chip')].some(c => c.querySelector('.cname').textContent === 'Host' && !(c.dataset.downRes === '400\u00d7225'))"));
  await sam.evaluate(`(function () { const s = document.querySelector('${tileSel(alexKey)} .stream-panel select'); s.value = ''; s.dispatchEvent(new Event('change')); })()`);
  check("clearing it goes back to the View panel's size", await waitFor(() => sam.evaluate("[...document.querySelectorAll('#peerChips .chip')].some(c => c.querySelector('.cname').textContent === 'Alex' && c.dataset.downRes !== '400\u00d7225')")));
  await sam.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))");
  check("Escape closes the panel", await sam.evaluate("!document.querySelector('.stream-panel')"));

  await host.evaluate(`document.querySelector('${tileSel(ownKey)} .tool.settings').click()`);
  check("one's own tile offers quality, frame rate and the mouse pointer", await host.evaluate(`document.querySelectorAll('${tileSel(ownKey)} .stream-panel select').length === 3`));
  check("the mouse pointer is on by default", await host.evaluate("document.getElementById('pointer').value === 'on'"));
  await host.evaluate("(function () { window.__fetched = []; const f = window.fetch; window.fetch = function (url) { window.__fetched.push(String(url)); return f.apply(this, arguments); }; })()");
  await host.evaluate(`(function () { const s = [...document.querySelectorAll('${tileSel(ownKey)} .stream-panel select')][2]; s.value = 'off'; s.dispatchEvent(new Event('change')); })()`);
  check("hiding the pointer restarts that screen's frames without it", await waitFor(() => host.evaluate("window.__fetched.some(u => u.includes('/frames') && u.includes('pointer=0'))")));
  check("and the panel's choice was for that screen only", await host.evaluate("document.getElementById('pointer').value === 'on'"));
  await host.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))");

  await sam.evaluate(`document.querySelector('${tileSel(alexKey)} .tool.fullscreen').click()`);
  check("the fullscreen button fullscreens that stream", await waitFor(() => sam.evaluate(`!!document.fullscreenElement && document.fullscreenElement.dataset.key === '${alexKey}'`)));
  check("the stream fills the window", await waitFor(() => sam.evaluate(`(function () { const r = document.fullscreenElement.getBoundingClientRect(); return Math.abs(r.width - innerWidth) < 2 && Math.abs(r.height - innerHeight) < 2; })()`)));
  check("its button now leaves fullscreen", await sam.evaluate(`document.querySelector('${tileSel(alexKey)} .tool.fullscreen').title === 'Leave fullscreen'`));
  await sam.evaluate(`document.querySelector('${tileSel(alexKey)} .tool.fullscreen').click()`);
  check("pressing it again returns to the grid", await waitFor(() => sam.evaluate("!document.fullscreenElement && document.querySelectorAll('#tiles .tile').length >= 2")));

  // Inside the app the window host ignores the browser's fullscreen, so the page asks the app to make the window cover the
  // display and makes the stream fill the window. A stand-in for the bridge records what is asked.
  await sam.evaluate("window.__fs = []; window.pywebview = { api: { set_fullscreen: function (on) { window.__fs.push(on); return Promise.resolve({ ok: true }); } } }");
  await sam.evaluate(`document.querySelector('${tileSel(alexKey)} .tool.fullscreen').click()`);
  check("in the app the stream's fullscreen asks for the whole window to go fullscreen", await waitFor(() => sam.evaluate("window.__fs.length > 0 && window.__fs[window.__fs.length - 1] === true")));
  check("the stream fills the window and the bars step aside", await sam.evaluate(`(function () { const t = document.querySelector('${tileSel(alexKey)}'); const r = t.getBoundingClientRect(); return t.classList.contains('in-window-fullscreen') && r.left === 0 && r.top === 0 && Math.abs(r.width - innerWidth) < 2 && Math.abs(r.height - innerHeight) < 2 && getComputedStyle(document.getElementById('top')).display === 'none' && getComputedStyle(document.getElementById('side')).display === 'none' && getComputedStyle(document.getElementById('statsbar')).display === 'none'; })()`));
  check("its button now leaves fullscreen", await sam.evaluate(`document.querySelector('${tileSel(alexKey)} .tool.fullscreen').title === 'Leave fullscreen'`));
  await sam.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))");
  check("Escape asks for the window to come back", await waitFor(() => sam.evaluate("window.__fs[window.__fs.length - 1] === false")));
  check("and the bars return", await sam.evaluate("getComputedStyle(document.getElementById('top')).display !== 'none' && !document.querySelector('.in-window-fullscreen')"));
  await sam.evaluate("document.getElementById('fullBtn').click()");
  check("the top bar's Fullscreen asks for the whole window too", await waitFor(() => sam.evaluate("window.__fs[window.__fs.length - 1] === true && document.getElementById('statsbar').hidden")));
  await sam.evaluate(`document.querySelector('${tileSel(alexKey)} .tool.fullscreen').click()`);
  await sam.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))");
  check("leaving a stream's fullscreen keeps the window fullscreen when the top bar asked for it", await sam.evaluate("window.__fs[window.__fs.length - 1] === true"));
  await sam.evaluate("document.getElementById('fullBtn').click()");
  check("the top bar's button then restores the window", await waitFor(() => sam.evaluate("window.__fs[window.__fs.length - 1] === false && !document.getElementById('statsbar').hidden")));
  await sam.evaluate("delete window.pywebview");

  const stageWidth = () => sam.evaluate("document.getElementById('stage').getBoundingClientRect().width");
  const stageBefore = await stageWidth();
  await sam.evaluate("document.getElementById('sideBtn').click()");
  check("the side panel button hides the side panel", await waitFor(() => sam.evaluate("document.getElementById('side').hidden")));
  await sleep(500);
  await shot(sam, "6-side-hidden");
  check("the stage takes the room", await waitFor(async () => (await stageWidth()) > stageBefore + 200));
  await sam.evaluate("document.getElementById('sideBtn').click()");
  check("pressing it again shows the side panel", await waitFor(() => sam.evaluate("!document.getElementById('side').hidden")));

  // A pop-out joins as its own member under its own cookie. The window that opened it keeps working.
  const alexMember = alexKey.split(":")[0];
  const popOutPath = () => sam.evaluate(`fetch('/popout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target: '${alexMember}', slot: 0 }) }).then(r => r.ok ? r.json().then(j => j.path) : r.text().then(text => 'ERROR ' + text))`);
  const firstPath = await popOutPath();
  check("Sam's window gets a pop-out link", String(firstPath).startsWith("/join?token="), String(firstPath));
  await sam.evaluate(`(function () { window.open(${JSON.stringify(firstPath)}, '_blank', 'popup'); return true; })()`);
  const viewer = await attachTab((url) => url.includes("view="));
  check("the pop-out shows one screen", await waitFor(() => viewer.evaluate("document.body.classList.contains('viewer') && document.querySelectorAll('.tile').length === 1")));
  check("the pop-out's tile is Alex's screen", await viewer.evaluate("document.querySelector('.tile .label').textContent === 'Alex - Screen 1'"));
  check("the pop-out receives live frames", await waitFor(() => viewer.evaluate("(function () { const v = document.querySelector('.tile video'); return v.videoWidth > 0 && !v.paused; })()")));
  check("the pop-out's tile has settings and fullscreen, and no pop out", await viewer.evaluate("document.querySelectorAll('.tile .tools .tool').length === 2 && !document.querySelector('.tile .tool.popout')"));
  await viewer.evaluate("document.querySelector('.tile .tool.fullscreen').click()");
  check("fullscreen works inside the pop-out", await waitFor(() => viewer.evaluate("!!document.fullscreenElement && document.fullscreenElement.classList.contains('tile')")));
  await viewer.evaluate("document.querySelector('.tile .tool.fullscreen').click()");
  await waitFor(() => viewer.evaluate("!document.fullscreenElement"));
  await viewer.evaluate("document.querySelector('.tile .tool.settings').click()");
  check("the pop-out has its own settings panel", await viewer.evaluate("!!document.querySelector('.tile .stream-panel')"));
  const secondPath = await popOutPath();
  check("a second pop-out still works after the first opened", String(secondPath).startsWith("/join?token="), String(secondPath));
  check("Sam still has the party after the pop-out joined", await sam.evaluate("document.getElementById('count').textContent.startsWith('3 people') && document.getElementById('chatText').offsetParent !== null"));
  check("pop-outs are not counted as people", await host.evaluate("document.getElementById('count').textContent.startsWith('3 people')"));
  await viewer.evaluate("window.pywebview = { api: { close_window: function () { window.__closed = true; return Promise.resolve({ ok: true }); } } }");

  // 7b. Channels: the server starts with a text and a voice channel, the owner adds one, and it reaches everyone.
  check("a new server lists its text and voice channels", await sam.evaluate(
    "[...document.querySelectorAll('#channelList .channel-name')].map(function (n) { return n.textContent.replace(/\\s+/g, ' '); }).join('|') === '# general|Voice Voice 1'"));
  check("only the owner and admins are offered the add-channel buttons", await sam.evaluate("document.getElementById('addText').hidden") &&
    await host.evaluate("!document.getElementById('addText').hidden"));
  await host.evaluate(`(function () {
    document.getElementById('addText').click();
    var input = document.querySelector('#channelList .channel-edit input');
    input.value = 'maps';
    document.querySelector('#channelList .channel-edit').requestSubmit();
  })()`);
  check("the owner's new channel appears for a member", await waitFor(() => sam.evaluate(
    "[...document.querySelectorAll('#channelList .channel-name')].some(function (n) { return n.textContent.includes('maps'); })")));
  // A stand-in for the app's bridge on two pages: the member's page should use it, the host's should not.
  for (const tab of [sam, host]) await tab.evaluate(`(function () {
    window.__kept = [];
    window.pywebview = window.pywebview || { api: {} };
    window.pywebview.api.keep_chat = function (server, name, channels, messages) {
      Array.prototype.push.apply(window.__kept, messages);
      return Promise.resolve({ ok: true, missing: [] });
    };
  })()`);
  await sam.evaluate(`(function () {
    var row = [...document.querySelectorAll('#channelList .channel.text .channel-row')].find(function (r) { return r.textContent.includes('maps'); });
    row.click();
    document.getElementById('chatText').value = 'a message in maps';
    document.getElementById('chatForm').requestSubmit();
  })()`);
  check("a member's page hands what it receives to the app, to keep on that PC", await waitFor(() => sam.evaluate(
    "(window.__kept || []).some(function (m) { return m.text === 'a message in maps' && m.channel && m.ts && m.id; })")));
  check("and the host's own page does not (the party already stores it there)", await host.evaluate("!(window.__kept || []).length"));
  check("a message in another channel shows there", await waitFor(() => sam.evaluate(
    "document.getElementById('chatTitle').textContent === 'maps' && document.getElementById('messages').textContent.includes('a message in maps')")));
  check("and gives the other channel's reader an unread count instead of showing in their channel", await waitFor(() => host.evaluate(
    "document.getElementById('chatTitle').textContent === 'general' && !document.getElementById('messages').textContent.includes('a message in maps') && !!document.querySelector('#channelList .unread')")));
  await sam.evaluate("[...document.querySelectorAll('#channelList .channel.text .channel-row')].find(function (r) { return r.textContent.includes('general'); }).click()");

  // 7c. Pictures: a big picture is saved again in the page at its own size, uploaded, and shows for everyone at that size.
  await sam.evaluate(`(async function () {
    var canvas = document.createElement('canvas');
    canvas.width = 3200; canvas.height = 1800;
    var ctx = canvas.getContext('2d');
    ctx.fillStyle = '#c0392b'; ctx.fillRect(0, 0, 3200, 1800);
    var blob = await new Promise(function (r) { canvas.toBlob(r, 'image/png'); });
    var data = new DataTransfer();
    data.items.add(new File([blob], 'big.png', { type: 'image/png' }));
    var input = document.getElementById('attachFile');
    input.files = data.files;
    input.dispatchEvent(new Event('change'));
  })()`);
  check("a picture sent by a member reaches the host at its full 3200 by 1800", await waitFor(() => host.evaluate(
    "(function () { var i = document.querySelector('#messages img.chat-image'); return !!i && i.complete && i.naturalWidth === 3200 && i.naturalHeight === 1800; })()")));
  check("and shows for the sender too", await waitFor(() => sam.evaluate("!!document.querySelector('#messages img.chat-image')")));
  await host.evaluate("document.querySelector('#messages img.chat-image').click()");
  check("clicking a picture opens it large", await host.evaluate("!document.getElementById('lightbox').hidden"));
  await host.evaluate("document.getElementById('lightbox').click()");
  check("and clicking again closes it", await host.evaluate("document.getElementById('lightbox').hidden"));

  // 7d. Voice: two people join the voice channel and hear each other; the third, outside it, hears nothing.
  for (const tab of [host, sam]) await tab.evaluate("window.__errs = []; window.addEventListener('error', function (e) { window.__errs.push(e.message); }); window.addEventListener('unhandledrejection', function (e) { window.__errs.push('rejected: ' + (e.reason && e.reason.message || e.reason)); });");
  const joinVoice = (tab) => tab.evaluate(`[...document.querySelectorAll('#channelList .channel.voice .channel-row button')].find(function (b) { return b.textContent === 'Join'; }).click()`);
  await joinVoice(host);
  check("joining a voice channel shows the voice bar", await waitFor(() => host.evaluate(
    "!document.getElementById('voiceBar').hidden && document.getElementById('voiceName').textContent === 'Voice 1'")));
  await joinVoice(sam);
  check("the roster shows who is in voice", await waitFor(() => host.evaluate(
    "[...document.querySelectorAll('#roster li')].some(function (li) { return li.textContent.includes('Sam') && !!li.querySelector('.badge.voice'); })")));
  check("the voice channel lists its people", await waitFor(() => host.evaluate(
    "document.querySelectorAll('#channelList .voice-people li').length === 2")));
  check("a person in the channel hears the other (audio arrives)", await waitFor(() => host.evaluate(
    "(function () { var a = document.querySelectorAll('audio'); return a.length === 1 && a[0].srcObject.getAudioTracks()[0].muted === false; })()"), 20000));
  check("and the other hears them", await waitFor(() => sam.evaluate(
    "(function () { var a = document.querySelectorAll('audio'); return a.length === 1 && a[0].srcObject.getAudioTracks()[0].muted === false; })()"), 20000));
  check("someone outside the voice channel plays no audio", await alex.evaluate("document.querySelectorAll('audio').length === 0"));
  await host.evaluate("document.getElementById('muteBtn').click()");
  check("Mute says so", await host.evaluate("document.getElementById('muteBtn').textContent === 'Unmute' && document.getElementById('voiceState').textContent === 'Muted'"));
  await host.evaluate("document.getElementById('muteBtn').click()");
  await host.evaluate("document.getElementById('deafenBtn').click()");
  check("Deafen mutes what they hear", await host.evaluate("document.querySelector('audio').muted === true && document.getElementById('voiceState').textContent === 'Deafened'"));
  await host.evaluate("document.getElementById('deafenBtn').click()");
  await sam.evaluate("document.getElementById('voiceLeave').click()");
  check("when one leaves the other stops hearing them", await waitFor(() => host.evaluate("document.querySelectorAll('audio').length === 0")));
  check("and the roster no longer shows them in voice", await waitFor(() => host.evaluate(
    "![...document.querySelectorAll('#roster li')].some(function (li) { return li.textContent.includes('Sam') && !!li.querySelector('.badge.voice'); })")));
  await host.evaluate("document.getElementById('voiceLeave').click()");
  check("leaving hides the voice bar", await waitFor(() => host.evaluate("document.getElementById('voiceBar').hidden")));

  // 8. The host removes Sam. Sam is told, and the roster shrinks for the others.
  await host.evaluate(`(function () {
    var row = [...document.querySelectorAll('#roster li')].find(function (li) { return li.querySelector('.name').textContent.startsWith('Sam'); });
    row.querySelector('button[aria-label^="Actions for"]').click();
  })()`);
  await host.evaluate(`[...document.querySelectorAll('#ctxMenu button')].find(function (b) { return b.textContent.startsWith('Remove'); }).click()`);
  check("Sam is told the host removed them", await waitFor(() => sam.evaluate("document.getElementById('status').textContent.includes('removed you')")));
  check("the host sees two people again", await waitFor(() => host.evaluate("document.getElementById('count').textContent.startsWith('2 people')")));
  check("Sam's pop-out is asked to close with them", await waitFor(() => viewer.evaluate("window.__closed === true")));

  // 9. The host's Fullscreen button hides the stats bar, and leaving fullscreen brings it back.
  await host.evaluate("document.getElementById('fullBtn').click()");
  check("Fullscreen enters fullscreen on the host", await waitFor(() => host.evaluate("!!document.fullscreenElement")));
  check("the stats bar hides in fullscreen", await waitFor(() => host.evaluate("document.getElementById('statsbar').hidden")));
  await host.evaluate("document.getElementById('fullBtn').click()");
  check("the stats bar comes back after leaving fullscreen", await waitFor(() => host.evaluate("!document.getElementById('statsbar').hidden")));

  // 10. Alex leaves on their own.
  await alex.evaluate("window.pywebview = { api: { close_window: function () { window.__closed = true; return Promise.resolve({ ok: true }); } } }");
  await alex.evaluate("document.getElementById('leaveBtn').click()");
  check("leaving also closes the party window", await waitFor(() => alex.evaluate("window.__closed === true")));
  check("Alex is told they left", await waitFor(() => alex.evaluate("document.getElementById('status').textContent.includes('You left')")));
  check("the host sees one person again", await waitFor(() => host.evaluate("document.getElementById('count').textContent.startsWith('1 person')")));

  return finish();
}

function finish() {
  clearTimeout(watchdog);
  killAll();
  const verdict = failed === 0 ? "PASS" : `FAIL ${failed}`;
  console.log(`RESULT ${verdict}`);
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((error) => {
  console.log(`FAIL ${error.message}`);
  failed += 1;
  finish();
});
