/*
 * What the bottom bar and its hover card say about each connection. Pure functions on plain objects, so the tests can
 * run them in Node: normalize() reduces one WebRTC stats report to the numbers that matter, diff() turns two of them
 * into rates, and the helpers format and pick what fits.
 *
 * A connection carries up to SLOTS video slots each way. A slot is "applicable" only while something flows through it,
 * so a number that does not apply is never shown.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PrivatePartyNetNetStats = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // One stats report, as an array of its entries, reduced to the video slots it describes. mids is the list of
  // transceiver mids in slot order, so a stream's mid says which slot it is.
  function normalize(entries, mids, now) {
    var sample = { at: now, rtt: null, jitter: null, way: way(entries), inn: {}, out: {}, voice: { packets: 0, lost: 0 } };
    entries.forEach(function (s) {
      if (s.type === "candidate-pair") {
        if (s.nominated && s.state === "succeeded" && typeof s.currentRoundTripTime === "number") {
          sample.rtt = s.currentRoundTripTime * 1000;
        }
        return;
      }
      if (s.type === "inbound-rtp" && !s.isRemote && typeof s.jitter === "number") {
        sample.jitter = Math.max(sample.jitter || 0, s.jitter * 1000); // the shakier of what arrives, in ms
      }
      if (s.type === "inbound-rtp" && s.kind === "audio" && !s.isRemote) {
        sample.voice.packets += s.packetsReceived || 0;
        sample.voice.lost += s.packetsLost || 0;
        return;
      }
      if (s.kind !== "video" || s.isRemote) return;
      var slot = mids.indexOf(s.mid);
      if (slot < 0) return;
      if (s.type === "inbound-rtp") {
        sample.inn[slot] = {
          bytes: s.bytesReceived || 0, packets: s.packetsReceived || 0, lost: s.packetsLost || 0,
          frames: s.framesDecoded || 0, w: s.frameWidth || 0, h: s.frameHeight || 0, busy: s.totalDecodeTime || 0,
        };
      } else if (s.type === "outbound-rtp") {
        sample.out[slot] = {
          bytes: s.bytesSent || 0, frames: s.framesEncoded || 0, w: s.frameWidth || 0, h: s.frameHeight || 0,
          busy: s.totalEncodeTime || 0, limit: s.qualityLimitationReason || "none",
        };
      }
    });
    return sample;
  }

  // Which way the connection in use goes, from its pair of candidates: "relay" through the group's relay server (TURN),
  // "ipv6" directly over IPv6, "lan" directly on one network, "direct" through the routers, or null before it is known.
  function way(entries) {
    var byId = {};
    var pair = null;
    entries.forEach(function (s) {
      if (s.type === "local-candidate" || s.type === "remote-candidate") byId[s.id] = s;
      if (s.type === "candidate-pair" && s.nominated && s.state === "succeeded") pair = s;
    });
    if (!pair) return null;
    var local = byId[pair.localCandidateId] || {};
    var remote = byId[pair.remoteCandidateId] || {};
    if (local.candidateType === "relay" || remote.candidateType === "relay") return "relay";
    var address = String(remote.address || remote.ip || "");
    if (address.indexOf(":") >= 0) return "ipv6";
    if (local.candidateType === "host" && remote.candidateType === "host") return "lan";
    return address ? "direct" : null;
  }

  var WAYS = {
    relay: "Through the relay server",
    ipv6: "Direct over IPv6",
    lan: "Direct on the same network",
    direct: "Direct through the routers",
  };

  function wayText(kind) {
    return WAYS[kind] || "";
  }

  function perSlot(prev, cur, secs, cores, active, outgoing) {
    var list = [];
    Object.keys(cur).forEach(function (key) {
      var slot = Number(key);
      var a = cur[key];
      var b = prev[key];
      if (!b || (active && !active.has(slot))) return;
      var item = {
        slot: slot,
        kbps: Math.max(0, ((a.bytes - b.bytes) * 8) / 1000 / secs),
        fps: Math.max(0, (a.frames - b.frames) / secs),
        w: a.w,
        h: a.h,
        cpu: Math.max(0, (a.busy - b.busy) / secs / cores * 100), // share of the whole PC, from encode or decode time
        packets: a.packets - (b.packets || 0),
        lost: a.lost - (b.lost || 0),
      };
      if (outgoing) item.limit = a.limit;
      list.push(item);
    });
    return list;
  }

  // Two reports a moment apart as rates. ctx: { cores, activeIn: Set of slots the other side is sharing, activeOut: Set
  // of slots this side is sharing }. Returns { rtt, jitter, loss, down: [slot], up: [slot] }; loss is the percentage of
  // the screens' and the voice's packets lost; a slot is { slot, kbps, fps, w, h, cpu } and an upload slot also has the
  // encoder's limit.
  function diff(prev, cur, ctx) {
    var result = { rtt: cur.rtt, jitter: cur.jitter, way: cur.way || null, loss: 0, down: [], up: [] };
    if (!prev) return result;
    var secs = (cur.at - prev.at) / 1000;
    if (secs <= 0) return result;
    var cores = (ctx && ctx.cores) || 1;
    result.down = perSlot(prev.inn, cur.inn, secs, cores, ctx && ctx.activeIn, false);
    result.up = perSlot(prev.out, cur.out, secs, cores, ctx && ctx.activeOut, true);
    var lost = 0;
    var received = 0;
    result.down.forEach(function (s) {
      lost += Math.max(0, s.lost);
      received += Math.max(0, s.packets);
    });
    if (cur.voice && prev.voice) {
      lost += Math.max(0, cur.voice.lost - prev.voice.lost);
      received += Math.max(0, cur.voice.packets - prev.voice.packets);
    }
    result.loss = received + lost > 0 ? (lost / (received + lost)) * 100 : 0;
    return result;
  }

  // The slots through which something is actually flowing.
  function flowing(slots) {
    return slots.filter(function (s) { return s.kbps > 1 || s.fps > 0.5; });
  }

  // One line for a direction: the total rate and CPU, the best frame rate, and the largest picture. null when nothing flows.
  function total(slots) {
    var live = flowing(slots);
    if (!live.length) return null;
    var out = { kbps: 0, fps: 0, w: 0, h: 0, cpu: 0, count: live.length, limit: "none" };
    live.forEach(function (s) {
      out.kbps += s.kbps;
      out.cpu += s.cpu;
      out.fps = Math.max(out.fps, s.fps);
      if (s.w * s.h > out.w * out.h) {
        out.w = s.w;
        out.h = s.h;
      }
      if (s.limit && s.limit !== "none") out.limit = s.limit;
    });
    return out;
  }

  function formatRate(kbps) {
    if (typeof kbps !== "number" || isNaN(kbps)) return "n/a";
    if (kbps >= 1000) return (kbps / 1000).toFixed(1) + " Mbps";
    return Math.round(kbps) + " kbps";
  }

  function formatCpu(percent) {
    if (typeof percent !== "number" || isNaN(percent)) return "n/a";
    return percent < 0.1 ? "<0.1%" : percent.toFixed(1) + "%";
  }

  function formatSize(w, h) {
    return w > 0 && h > 0 ? w + "×" + h : "";
  }

  // The highest detail level whose rendering fits. levels is ordered from the most detailed to the least; fits(level)
  // says whether that level fits the space. The last level is the answer when nothing fits.
  function highestFitting(levels, fits) {
    for (var i = 0; i < levels.length; i += 1) {
      if (fits(levels[i])) return levels[i];
    }
    return levels[levels.length - 1];
  }

  return {
    normalize: normalize,
    diff: diff,
    way: way,
    wayText: wayText,
    flowing: flowing,
    total: total,
    formatRate: formatRate,
    formatCpu: formatCpu,
    formatSize: formatSize,
    highestFitting: highestFitting,
  };
});
