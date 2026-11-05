/*
 * WebRTC helpers shared by the party page: screens (four video slots per pair) and voice (one audio slot per pair).
 *
 * A classic script, so the browser self-test can load it from a file path. Nothing here talks to the server:
 * the party page does its own signaling through the server's relay.
 */
(function (global) {
  "use strict";

  // On one network the browsers reach each other directly and need no STUN server. Once someone has joined from outside,
  // the page turns STUN on (setStun) so each side learns the public address its router gives it and the two can reach
  // each other through their routers. When the owner set up the group's relay server, the party also hands each page
  // TURN credentials for the coturn beside it (short-lived, made for this member): media between two routers that let
  // nothing through goes there, still encrypted end to end. A direct way is always preferred when one works.
  var STUN_SERVERS = [{ urls: ["stun:stun.l.google.com:19302", "stun:stun.cloudflare.com:3478"] }];
  var CONFIG = { iceServers: [] };

  function cleanTurn(list) {
    return (Array.isArray(list) ? list : []).filter(function (server) {
      return server && Array.isArray(server.urls) && typeof server.username === "string" &&
        typeof server.credential === "string" && server.urls.every(function (url) {
          return typeof url === "string" && /^turns?:/.test(url);
        });
    }).map(function (server) {
      return { urls: server.urls.slice(), username: server.username, credential: server.credential };
    });
  }

  function setStun(on, turn) {
    CONFIG = { iceServers: on ? STUN_SERVERS.concat(cleanTurn(turn)) : [] };
  }

  function iceServers() {
    return CONFIG.iceServers;
  }

  function stunOn() {
    return CONFIG.iceServers.length > 0;
  }
  var MIN_FIT = 160; // a minimized or tiny tile still asks for a sensible size, not zero
  // Each person can share this many screens at once. Keep in step with SHARE_SLOTS in server.py.
  var SLOTS = 4;

  // Each pair of members negotiates SLOTS video slots, sendrecv, once. Slot n carries share n of the sender. A
  // share goes into its slot with replaceTrack, so starting or stopping a share never needs a new negotiation.
  function newPeer() {
    return new RTCPeerConnection(CONFIG);
  }

  // Which video codec to prefer, from the page's saved choice ("h264", "vp9", "av1", "vp8"; anything else leaves the
  // browser's own order). Both ends apply it before negotiating, and the codec both support first in the list is used.
  function preferCodec(pc) {
    var wanted = "";
    try {
      wanted = (global.localStorage.getItem("privatepartynet.codec") || "").toLowerCase();
    } catch (err) {
      return;
    }
    if (!wanted || !global.RTCRtpSender || !RTCRtpSender.getCapabilities) return;
    var capabilities = RTCRtpSender.getCapabilities("video");
    if (!capabilities) return;
    var mime = "video/" + wanted;
    var first = capabilities.codecs.filter(function (c) { return c.mimeType.toLowerCase() === mime; });
    if (first.length === 0) return;
    var rest = capabilities.codecs.filter(function (c) { return c.mimeType.toLowerCase() !== mime; });
    pc.getTransceivers().forEach(function (transceiver) {
      if (transceiver.setCodecPreferences) transceiver.setCodecPreferences(first.concat(rest));
    });
  }

  function addSlots(pc) {
    for (var n = 0; n < SLOTS; n += 1) pc.addTransceiver("video", { direction: "sendrecv" });
    preferCodec(pc);
  }

  // The answering side takes its slots from the offer, in the same order. Mark them sendrecv before the answer.
  function prepareAnswer(pc) {
    pc.getTransceivers().forEach(function (transceiver) {
      transceiver.direction = "sendrecv";
    });
    preferCodec(pc);
  }

  // Each pair also negotiates one audio slot, after the video slots, so the video slot numbers do not move. It is
  // negotiated once, with no microphone in it. Joining a voice channel puts the microphone's track into it with
  // replaceTrack and leaving takes it out, so voice never needs a new negotiation either.
  function addAudio(pc) {
    pc.addTransceiver("audio", { direction: "sendrecv" });
  }

  function audioAt(pc) {
    var all = pc.getTransceivers();
    for (var i = 0; i < all.length; i += 1) {
      if (all[i].receiver.track && all[i].receiver.track.kind === "audio") return all[i];
    }
    return null;
  }

  // Put the microphone's track into the audio slot. Passing null stops sending voice to that peer.
  function setMicTrack(pc, track) {
    var transceiver = audioAt(pc);
    return transceiver ? transceiver.sender.replaceTrack(track) : Promise.resolve();
  }

  function remoteAudioTrack(pc) {
    var transceiver = audioAt(pc);
    return transceiver ? transceiver.receiver.track : null;
  }

  // Opus settings for speech on a LAN, written into an offer or answer: 32 kbps mono with forward error correction,
  // and silence not sent at all (DTX), so a quiet person costs almost nothing. Only the Opus lines change.
  var OPUS_SETTINGS = { minptime: "10", useinbandfec: "1", usedtx: "1", stereo: "0", "sprop-stereo": "0", maxaveragebitrate: "32000" };

  // Whether two session descriptions come from the same peer connection: the same DTLS certificate (a=fingerprint).
  // An ICE restart keeps it, so the offer is answered on the connection that is there, keeping its audio and screens; a
  // page that was reloaded has a new one, and the connection is made over.
  function sameEndpoint(oldSdp, newSdp) {
    function fingerprint(sdp) {
      var match = /^a=fingerprint:(.+)$/m.exec(String(sdp || ""));
      return match ? match[1].trim().toLowerCase() : null;
    }
    var before = fingerprint(oldSdp);
    return before !== null && before === fingerprint(newSdp);
  }

  // An ICE restart: a new offer with fresh ICE credentials on the same connection, which keeps its tracks, its audio
  // element and its screens while it finds a new way through (after a dropout, or a new address). An offer of ours that
  // was never answered is taken back first. `send(sdp)` delivers the offer; it rejects while the party is unreachable.
  async function restartIce(pc, send) {
    if (pc.signalingState === "closed") return false;
    if (pc.signalingState === "have-local-offer") await pc.setLocalDescription({ type: "rollback" });
    var offer = await pc.createOffer({ iceRestart: true });
    offer.sdp = tuneOpus(offer.sdp);
    await pc.setLocalDescription(offer);
    await gatherComplete(pc);
    await send(pc.localDescription.sdp);
    return true;
  }

  // Answer an offer on the connection that is there, when it comes from the same peer connection (sameEndpoint): an
  // ICE restart. Resolves false, changing nothing, when it does not: the caller then makes the connection over.
  async function answerInPlace(pc, sdp, send) {
    if (pc.signalingState === "closed" || !pc.remoteDescription || !sameEndpoint(pc.remoteDescription.sdp, sdp)) return false;
    if (pc.signalingState === "have-local-offer") await pc.setLocalDescription({ type: "rollback" });
    await pc.setRemoteDescription({ type: "offer", sdp: sdp });
    var answer = await pc.createAnswer();
    answer.sdp = tuneOpus(answer.sdp);
    await pc.setLocalDescription(answer);
    await gatherComplete(pc);
    await send(pc.localDescription.sdp);
    return true;
  }

  function tuneOpus(sdp) {
    var match = /a=rtpmap:(\d+) opus\/48000\/2/i.exec(sdp);
    if (!match) return sdp;
    var type = match[1];
    var lines = sdp.split("\r\n");
    var fmtp = -1;
    var rtpmap = -1;
    for (var i = 0; i < lines.length; i += 1) {
      if (lines[i].indexOf("a=fmtp:" + type + " ") === 0) fmtp = i;
      if (lines[i].indexOf("a=rtpmap:" + type + " ") === 0) rtpmap = i;
    }
    var params = {};
    var order = [];
    if (fmtp >= 0) {
      lines[fmtp].slice(("a=fmtp:" + type + " ").length).split(";").forEach(function (pair) {
        var at = pair.indexOf("=");
        if (at > 0) {
          params[pair.slice(0, at).trim()] = pair.slice(at + 1).trim();
          order.push(pair.slice(0, at).trim());
        }
      });
    }
    Object.keys(OPUS_SETTINGS).forEach(function (key) {
      if (!(key in params)) order.push(key);
      params[key] = OPUS_SETTINGS[key];
    });
    var line = "a=fmtp:" + type + " " + order.map(function (key) { return key + "=" + params[key]; }).join(";");
    if (fmtp >= 0) {
      lines[fmtp] = line;
    } else if (rtpmap >= 0) {
      lines.splice(rtpmap + 1, 0, line);
    }
    return lines.join("\r\n");
  }

  function slotAt(pc, slot) {
    var transceiver = pc.getTransceivers()[slot];
    return transceiver && transceiver.receiver.track.kind === "video" ? transceiver : null;
  }

  // The track a member sends in one slot, as this side receives it. Null when the slot does not exist.
  function remoteTrack(pc, slot) {
    var transceiver = slotAt(pc, slot);
    return transceiver ? transceiver.receiver.track : null;
  }

  // Put a share's track into one slot. Passing null stops sending in that slot.
  function setScreenTrack(pc, slot, track) {
    var transceiver = slotAt(pc, slot);
    return transceiver ? transceiver.sender.replaceTrack(track) : Promise.resolve();
  }

  // Limit what this side sends in one slot to one receiver. The receiver's choice (quality) and this side's Go
  // Live ceiling (ceiling) both apply; whichever is stricter wins. Scaling happens in the encoder, so bandwidth drops.
  //   quality: { mode: "percent", percent: 100 | 75 | 50 | 25 }
  //         or { mode: "fit", width, height }      (the receiver's tile, in physical pixels)
  //         or { mode: "off" }                    (the receiver is not showing this screen; send nothing)
  //   ceiling: { height: 1080 | 720 | 480 | null, fps: 15 | 30 | 60 | null }
  // Never upscales. Returns the scale used.
  // source is the screen's real size when the track is a smaller capture of it (the page captures at the size the viewers
  // need): a viewer's percentage and tile are measured against the real screen, and the scale is the track's size over the
  // size wanted, never below 1.
  async function limitSending(pc, slot, track, quality, ceiling, source) {
    var transceiver = slotAt(pc, slot);
    if (!transceiver || !track) return 1;
    var params = transceiver.sender.getParameters();
    if (!params.encodings || params.encodings.length === 0) params.encodings = [{}];
    if (quality && quality.mode === "off") {
      // the receiver chose not to show this screen: stop sending it to them until they ask again
      params.encodings[0].active = false;
      await transceiver.sender.setParameters(params);
      return 0;
    }
    params.encodings[0].active = true;
    var settings = track.getSettings();
    var scale = 1;
    if (settings.width && settings.height) {
      var realWidth = (source && source.width) || settings.width;
      var realHeight = (source && source.height) || settings.height;
      var target = realHeight; // the height of the picture to send
      if (quality && quality.mode === "percent") {
        target = (realHeight * quality.percent) / 100;
      } else if (quality && quality.mode === "fit") {
        target = realHeight / Math.max(1, realWidth / quality.width, realHeight / quality.height);
      }
      if (ceiling && ceiling.height) target = Math.min(target, ceiling.height);
      scale = settings.height / target;
    } else if (quality && quality.mode === "percent") {
      scale = 100 / quality.percent;
    }
    scale = Math.max(1, scale);
    params.encodings[0].scaleResolutionDownBy = scale;
    if (ceiling && ceiling.fps) params.encodings[0].maxFramerate = ceiling.fps;
    // Keep the chosen size. Without this the browser shrinks the picture by itself on a slow link. Under
    // congestion the frame rate drops instead, which suits a screen.
    params.degradationPreference = "maintain-resolution";
    await transceiver.sender.setParameters(params);
    return scale;
  }

  // The size of an element on screen, in physical pixels. A receiver's "Fit my window" uses this, so the
  // picture follows the window when it is resized, maximized, or minimized.
  function fitTo(element) {
    var ratio = global.devicePixelRatio || 1;
    var box = element.getBoundingClientRect();
    return {
      mode: "fit",
      width: Math.max(MIN_FIT, Math.round(box.width * ratio)),
      height: Math.max(MIN_FIT, Math.round(box.height * ratio)),
    };
  }

  function percentQuality(value) {
    return { mode: "percent", percent: Number(value) };
  }

  // Resolve once every ICE candidate is gathered, so the SDP sent over the relay lists them all.
  function gatherComplete(pc, timeoutMs) {
    return new Promise(function (resolve) {
      if (pc.iceGatheringState === "complete") {
        resolve();
        return;
      }
      var timer = setTimeout(finish, timeoutMs || 5000);
      function check() {
        if (pc.iceGatheringState === "complete") finish();
      }
      function finish() {
        clearTimeout(timer);
        pc.removeEventListener("icegatheringstatechange", check);
        resolve();
      }
      pc.addEventListener("icegatheringstatechange", check);
    });
  }

  // The counters the stats lines need, from one getStats() report. Every slot counts toward the byte totals. The
  // picture size comes from the first slot that has a frame, and a slot with no frame yet has no size.
  function readReports(reports) {
    var sample = { at: performance.now(), bytes: { "down-video": 0, "up-video": 0, "down-audio": 0, "up-audio": 0 }, down: null, up: null, rttMs: null, lost: 0 };
    reports.forEach(function (report) {
      report.forEach(function (stat) {
      if (stat.type === "inbound-rtp" && !stat.isRemote && stat.kind === "video") {
        sample.bytes["down-video"] += stat.bytesReceived || 0;
        sample.lost += stat.packetsLost || 0;
        if (!sample.down && stat.frameWidth) sample.down = { fps: stat.framesPerSecond, width: stat.frameWidth, height: stat.frameHeight };
      } else if (stat.type === "outbound-rtp" && !stat.isRemote && stat.kind === "video") {
        sample.bytes["up-video"] += stat.bytesSent || 0;
        if (!sample.up && stat.frameWidth) sample.up = { fps: stat.framesPerSecond, width: stat.frameWidth, height: stat.frameHeight };
      } else if (stat.type === "inbound-rtp" && !stat.isRemote && stat.kind === "audio") {
        sample.bytes["down-audio"] += stat.bytesReceived || 0;
      } else if (stat.type === "outbound-rtp" && !stat.isRemote && stat.kind === "audio") {
        sample.bytes["up-audio"] += stat.bytesSent || 0;
      } else if (stat.type === "candidate-pair" && stat.nominated && stat.state === "succeeded") {
        if (typeof stat.currentRoundTripTime === "number") sample.rttMs = stat.currentRoundTripTime * 1000;
      }
      });
    });
    return sample;
  }

  function readReport(report) {
    return readReports([report]);
  }

  // Kilobits per second for each byte counter, measured against the previous sample.
  function rates(sample, previous) {
    var out = {};
    if (!previous) return out;
    var seconds = (sample.at - previous.at) / 1000;
    if (seconds <= 0) return out;
    Object.keys(sample.bytes).forEach(function (key) {
      var before = previous.bytes[key];
      if (before !== undefined) out[key] = ((sample.bytes[key] - before) * 8) / 1000 / seconds;
    });
    return out;
  }

  function formatRate(kbps) {
    if (typeof kbps !== "number" || isNaN(kbps)) return "n/a";
    return kbps >= 1000 ? (kbps / 1000).toFixed(1) + " Mbps" : Math.round(kbps) + " kbps";
  }

  // A one-line description of one direction, for the stats bar: "Down from Alex: 1.2 Mbps, 30 fps, 1280x720".
  function describeLine(label, kbps, detail) {
    var parts = [formatRate(kbps)];
    if (detail && typeof detail.fps === "number") parts.push(Math.round(detail.fps) + " fps");
    if (detail && detail.width) parts.push(detail.width + "x" + detail.height);
    return label + ": " + parts.join(", ");
  }

  // Wire a fullscreen button and hide the stats bar while the page is fullscreen.
  function bindFullscreen(button, bar) {
    button.addEventListener("click", function () {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        document.documentElement.requestFullscreen();
      }
    });
    document.addEventListener("fullscreenchange", function () {
      bar.hidden = document.fullscreenElement === document.documentElement; // one stream going fullscreen leaves the bar
    });
  }

  global.PartyRTC = {
    SLOTS: SLOTS,
    newPeer: newPeer,
    addSlots: addSlots,
    setStun: setStun,
    stunOn: stunOn,
    iceServers: iceServers,
    addAudio: addAudio,
    setMicTrack: setMicTrack,
    remoteAudioTrack: remoteAudioTrack,
    tuneOpus: tuneOpus,
    sameEndpoint: sameEndpoint,
    restartIce: restartIce,
    answerInPlace: answerInPlace,
    OPUS_SETTINGS: OPUS_SETTINGS,
    prepareAnswer: prepareAnswer,
    remoteTrack: remoteTrack,
    setScreenTrack: setScreenTrack,
    limitSending: limitSending,
    fitTo: fitTo,
    percentQuality: percentQuality,
    gatherComplete: gatherComplete,
    readReport: readReport,
    readReports: readReports,
    rates: rates,
    formatRate: formatRate,
    describeLine: describeLine,
    bindFullscreen: bindFullscreen,
  };
})(typeof window !== "undefined" ? window : globalThis);
