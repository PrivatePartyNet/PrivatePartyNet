/*
 * The party page's side of staying in the party through bad internet (3.3).
 *
 * The host pings every member's page every 2 s on the event stream, and the page answers (POST /pong). The page times
 * its own answers, which gives "my link": the round trip to the host, the loss over the last 30 s, and a state that a
 * single late answer does not change: "unstable" after 4 s without a word from the host, "reconnecting" after 10 s,
 * and "ok" again after 3 answers in a row, as the host counts it for everyone (linkstate.py). After 10 s the page stops
 * waiting on its event stream and reconnects by itself, with growing pauses, for as long as the reconnect grace lasts.
 *
 * Pure functions and a small meter on plain numbers, with no page in sight, so the tests can run them in Node.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PrivatePartyNetLink = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var UNSTABLE_MS = 4000;
  var RECONNECTING_MS = 10000;
  var GOOD_BEATS = 3;
  var WINDOW_MS = 30000;
  var BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 10000];
  var GRACE_MS = 120000;

  // The pause before reconnect try number `attempt` (0, 1, ...): 0.5 s, 1, 2, 4, 8, then 10 s, each +-20%.
  function backoff(attempt, rnd) {
    var base = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    return Math.round(base * (0.8 + 0.4 * (rnd === undefined ? Math.random() : rnd)));
  }

  function Meter(now, wall) {
    this.state = "ok";
    this.since = Math.floor((wall === undefined ? now : wall) / 1000);
    this.heard = now;
    this.rtt = null;
    this.streak = 0;
    this.results = [];  // [at, answered]
    this.path = null;
    this.seen = false;  // a ping has arrived: the host measures this page (the host's own page never gets one)
  }

  // Anything from the host (a message on the event stream) shows the connection is alive.
  Meter.prototype.heardFrom = function (now) {
    this.heard = Math.max(this.heard, now);
  };

  // An answer to a ping came back after `ms`.
  Meter.prototype.answered = function (ms, now) {
    this.rtt = this.rtt === null ? ms : this.rtt * 0.75 + ms * 0.25;
    this.results.push([now, true]);
    this.heard = Math.max(this.heard, now);
    this.streak += 1;
    this.trim(now);
  };

  // An answer did not go through (the request failed or took too long).
  Meter.prototype.missed = function (now) {
    this.results.push([now, false]);
    this.streak = 0;
    this.trim(now);
  };

  Meter.prototype.trim = function (now) {
    while (this.results.length && now - this.results[0][0] > WINDOW_MS) this.results.shift();
  };

  Meter.prototype.loss = function () {
    if (!this.results.length) return 0;
    var lost = this.results.filter(function (r) { return !r[1]; }).length;
    return Math.round((lost / this.results.length) * 1000) / 1000;
  };

  // Move the state on from how long the host has been silent. True when it changed.
  Meter.prototype.update = function (now, wall) {
    var silent = now - this.heard;
    var state = this.state;
    if (silent >= RECONNECTING_MS) state = "reconnecting";
    else if (silent >= UNSTABLE_MS) { if (state === "ok") state = "unstable"; }
    else if (state !== "ok" && this.streak >= GOOD_BEATS) state = "ok";
    if (silent >= UNSTABLE_MS) this.streak = 0;
    if (state === this.state) return false;
    this.state = state;
    this.since = Math.floor((wall === undefined ? now : wall) / 1000);
    return true;
  };

  Meter.prototype.view = function () {
    var link = { state: this.state, rtt: this.rtt === null || this.state === "reconnecting" ? null : Math.round(this.rtt),
                 loss: this.loss(), since: this.since };
    if (this.path) link.path = this.path;
    return link;
  };

  // "general:12,voice-text:3": the newest message seen in each channel, for /events?since= after a dropout.
  function sinceParam(lastSeq) {
    return Object.keys(lastSeq || {}).filter(function (id) { return lastSeq[id] > 0 && /^[\w-]{1,64}$/.test(id); })
      .map(function (id) { return id + ":" + lastSeq[id]; }).join(",");
  }

  // The banner the party window shows while it reconnects, or null for none.
  function banner(state, awayMs, graceMs) {
    if (state === "reconnecting") {
      return "Reconnecting… (" + Math.max(1, Math.round(awayMs / 1000)) + " s)";
    }
    if (state === "back") return "Reconnected";
    if (state === "lost") {
      return "The connection to the party could not be restored" + (graceMs ? " within " +
        Math.round(graceMs / 1000) + " seconds" : "") + ". Your place has ended; press Rejoin to ask again.";
    }
    return null;
  }

  // Whether a screen should be sent smaller now: while this page's link, or the viewer's, is having a hard time.
  function linkIsBad(link) {
    return !!link && (link.state === "unstable" || link.state === "reconnecting" || (link.loss || 0) >= 0.2);
  }
  var BAD_LINK_HEIGHT = 480;

  return {
    UNSTABLE_MS: UNSTABLE_MS, RECONNECTING_MS: RECONNECTING_MS, GOOD_BEATS: GOOD_BEATS, GRACE_MS: GRACE_MS,
    BAD_LINK_HEIGHT: BAD_LINK_HEIGHT, backoff: backoff, Meter: Meter, sinceParam: sinceParam, banner: banner,
    linkIsBad: linkIsBad
  };
});
