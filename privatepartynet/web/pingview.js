/*
 * Each member's ping, as the party room's members list, the connection bar and their hover cards show it. Pure functions
 * on plain objects, so the tests run them in Node.
 *
 * Where a ping comes from, best first:
 *   "direct"  this page's own WebRTC connection to that member (its candidate pair's round trip, and loss and jitter
 *             from what arrives): the true you-to-them ping;
 *   "host"    the host's heartbeat with that member, every 2 seconds (the member's "link" in the party's list): their
 *             ping to the host, so through the host;
 *   "mylink"  your own row, and a member's row for the host: your ping to the host;
 *   "none"    nothing to go on (a host on an older PrivatePartyNet sends no "link").
 * Colours: green under 80 ms, yellow from 80, red from 150 or with more than 5% lost, grey when not known.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PrivatePartyNetPingView = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var GOOD = 80; // ms: under this is green
  var POOR = 150; // ms: from this is red
  var LOSSY = 0.05; // share of packets: more than this lost is red

  function number(value) {
    return typeof value === "number" && isFinite(value) && value >= 0;
  }

  // "good", "fair", "poor" or "unknown", from a ping in ms and the share of packets lost (0 to 1).
  function quality(rtt, loss) {
    if (number(loss) && loss > LOSSY) return "poor";
    if (!number(rtt)) return "unknown";
    if (rtt >= POOR) return "poor";
    return rtt >= GOOD ? "fair" : "good";
  }

  function fromLink(link, source) {
    if (!link || typeof link !== "object") return null;
    var state = link.state === "unstable" || link.state === "reconnecting" ? link.state : "ok";
    return {
      state: state, rtt: number(link.rtt) ? link.rtt : null, loss: number(link.loss) ? Math.min(1, link.loss) : null,
      jitter: null, source: source, way: typeof link.path === "string" ? "link-" + link.path : null,
    };
  }

  // The ping to show for `o.member`. o: { member, summary (this page's connection summary for them, if any), myLink
  // (this member's own link to the host), self (the row is this page's person), selfIsHost, isHost (the row is the
  // host's) }. Returns { state: "ok"|"unstable"|"reconnecting"|"self", rtt, loss, jitter, source, way }.
  function pick(o) {
    var member = o.member || {};
    if (o.self) {
      if (o.selfIsHost) return { state: "self", rtt: null, loss: null, jitter: null, source: "self", way: null };
      return fromLink(o.myLink || member.link, "mylink") || none();
    }
    // The host's own entry says 0 ms (it is the host): for its row, your own link to it stands in.
    var hostView = o.isHost ? null : fromLink(member.link, "host");
    var summary = o.summary;
    if (summary && summary.state === "connected" && number(summary.rtt)) {
      return {
        state: hostView && hostView.state !== "ok" ? hostView.state : "ok",
        rtt: summary.rtt, loss: number(summary.loss) ? summary.loss / 100 : null,
        jitter: number(summary.jitter) ? summary.jitter : null, source: "direct", way: summary.way || null,
      };
    }
    if (hostView && (hostView.rtt !== null || hostView.state !== "ok")) return hostView;
    if (o.isHost) return fromLink(o.myLink, "mylink") || none();
    return hostView || none();
  }

  function none() {
    return { state: "ok", rtt: null, loss: null, jitter: null, source: "none", way: null };
  }

  // What the members list writes beside a name.
  function text(choice) {
    if (choice.state === "self") return "";
    if (choice.state === "reconnecting") return "reconnecting…";
    if (choice.state === "unstable") return "unstable";
    return number(choice.rtt) ? Math.round(choice.rtt) + " ms" : "—";
  }

  function lossText(loss) {
    return number(loss) ? (loss * 100).toFixed(1) + "%" : "n/a";
  }

  // The ways a connection can go: WebRTC's (netstats.js way()), and the host's heartbeat's "path" (as "link-" + path).
  // Inside a tunnel the host tells only punched from relayed, so a link's "relay" may be either kind of relay.
  var WAYS = {
    lan: "Direct on the same network",
    ipv6: "Direct over IPv6",
    direct: "Direct through the routers",
    relay: "Through the relay server",
    "link-direct-lan": "Direct on the same network",
    "link-direct-ipv6": "Direct over IPv6",
    "link-direct": "Direct to a public address or an opened port",
    "link-punched": "Direct through both routers",
    "link-relay": "Through a relay (the relay server or a member's PC)",
    "link-friend": "Through a member's PC",
  };

  function wayText(kind) {
    return WAYS[kind] || "";
  }

  // The hover card's lines for one member's ping. `name` is how to call them ("Alex").
  function detail(choice, name) {
    if (choice.state === "self") return ["You are the host: your ping is everyone else's."];
    if (choice.state !== "ok") {
      var whose = choice.source === "mylink" ? "Your connection to the host" : name + "'s connection to the host";
      return [whose + " is " + choice.state + "."];
    }
    if (!number(choice.rtt)) return ["Ping not known yet (the host's PrivatePartyNet may be older)."];
    var first = "Ping " + Math.round(choice.rtt) + " ms";
    if (number(choice.jitter)) first += "  ·  jitter " + Math.round(choice.jitter) + " ms";
    first += "  ·  loss " + lossText(choice.loss === null ? 0 : choice.loss);
    var lines = [first];
    lines.push(wayText(choice.way) || (choice.source === "direct" ? "" : "Through the host"));
    if (choice.source === "direct") lines.push("Measured on your own connection to " + name + ".");
    else if (choice.source === "mylink") lines.push("Your ping to the host, which measures it every 2 seconds.");
    else lines.push(name + "'s ping to the host, which measures it every 2 seconds (you have no direct connection to " +
      name + ").");
    return lines.filter(Boolean);
  }

  // The connection bar's network line and dot, from a connection summary (loss there is a percentage), with the members
  // list's wording and colours.
  function summaryLine(summary) {
    return "Ping " + (number(summary.rtt) ? Math.round(summary.rtt) + " ms" : "n/a") + "  ·  loss " +
      lossText(number(summary.loss) ? summary.loss / 100 : 0);
  }

  function summaryQuality(summary) {
    if (!summary || summary.state !== "connected") return "unknown";
    return quality(summary.rtt, number(summary.loss) ? summary.loss / 100 : null);
  }

  // One member on the control panel's game network line: { name, way, rtt } (app.py _paths_view) as its words and
  // colour, the same thresholds as the party room's.
  function gameLine(member) {
    var shown = number(member.rtt);
    return { text: member.name + ", " + member.way + (shown ? ", " + Math.round(member.rtt) + " ms" : ""),
             quality: shown ? quality(member.rtt, null) : "unknown" };
  }

  return {
    GOOD: GOOD, POOR: POOR, LOSSY: LOSSY, gameLine: gameLine,
    quality: quality, pick: pick, text: text, lossText: lossText, wayText: wayText, detail: detail,
    summaryLine: summaryLine, summaryQuality: summaryQuality,
  };
});
