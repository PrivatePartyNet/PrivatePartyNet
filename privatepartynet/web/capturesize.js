/*
 * How large a picture a shared screen needs to be captured at.
 *
 * Capturing a 3440x1440 screen, JPEG-encoding every frame, decoding it again in the page and drawing it costs a lot of
 * processor time, and it is wasted when everyone watching has the screen in a small tile. The page asks this module what
 * the viewers need and captures at the smallest standard size that covers them. Pure functions on plain numbers, with no
 * page in sight, so the tests can run them in Node.
 *
 *   source:   { width, height }  the screen's real size
 *   quality:  what one viewer asked for, as RTC.limitSending reads it:
 *               { mode: "fit", width, height }  the viewer's tile, in physical pixels
 *               { mode: "percent", percent }    a share of the screen's real size
 *               { mode: "off" }                 the viewer is not showing it
 *               undefined                       the viewer has not said yet, so the full size is assumed
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PrivatePartyNetCaptureSize = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var STEPS = [240, 360, 480, 720, 1080, 1440, 2160];
  var SETTLE_MS = 5000; // a smaller size is taken only after the need has stayed lower for this long
  var HEADROOM = 1.0; // the capture is at least as tall as the tallest picture asked for

  // The height, in pixels, of the picture one viewer needs. Never more than the screen's own height.
  function wantedHeight(source, quality) {
    if (!quality) return source.height;
    if (quality.mode === "off") return 0;
    if (quality.mode === "percent") return Math.min(source.height, (source.height * Number(quality.percent)) / 100);
    if (quality.mode === "fit" && quality.width > 0 && quality.height > 0) {
      var scale = Math.max(1, source.width / quality.width, source.height / quality.height);
      return source.height / scale;
    }
    return source.height;
  }

  // The height the screen must be captured at so every viewer gets what they asked for. ceilingHeight is the sender's own
  // quality limit (or null). With nobody watching, the smallest size is enough.
  function neededHeight(source, qualities, ceilingHeight) {
    var need = 0;
    qualities.forEach(function (q) {
      need = Math.max(need, wantedHeight(source, q));
    });
    if (ceilingHeight) need = Math.min(need, ceilingHeight);
    return Math.max(STEPS[0], need * HEADROOM);
  }

  // The smallest standard size that covers need, or null when the whole screen is needed (capture it unscaled).
  function stepFor(need, sourceHeight) {
    if (!sourceHeight || need >= sourceHeight * 0.95) return null;
    for (var i = 0; i < STEPS.length; i += 1) {
      if (STEPS[i] >= need && STEPS[i] < sourceHeight * 0.95) return STEPS[i];
    }
    return null;
  }

  // Decides when to change the capture size. A larger size is taken at once, because a viewer is waiting for it. A smaller
  // one only after the need has stayed lower for SETTLE_MS, and then at the largest need seen meanwhile, so a window being
  // dragged does not make the capture restart over and over. null stands for the full, unscaled screen.
  function Governor(current) {
    this.current = current === undefined ? null : current;
    this.lowSince = null;
    this.peak = 0;
  }

  function rank(step) {
    return step === null ? Infinity : step;
  }

  // Returns { height, changed }.
  Governor.prototype.decide = function (step, now) {
    var want = rank(step);
    var have = rank(this.current);
    if (want > have) {
      this.current = step;
      this.lowSince = null;
      this.peak = 0;
      return { height: this.current, changed: true };
    }
    if (want < have) {
      if (this.lowSince === null) {
        this.lowSince = now;
        this.peak = 0;
      }
      this.peak = Math.max(this.peak, want);
      if (now - this.lowSince >= SETTLE_MS) {
        var settled = this.peak;
        this.lowSince = null;
        this.peak = 0;
        this.current = settled === Infinity ? null : settled;
        return { height: this.current, changed: rank(this.current) !== have };
      }
      return { height: this.current, changed: false };
    }
    this.lowSince = null;
    this.peak = 0;
    return { height: this.current, changed: false };
  };

  return { STEPS: STEPS, SETTLE_MS: SETTLE_MS, wantedHeight: wantedHeight, neededHeight: neededHeight, stepFor: stepFor, Governor: Governor };
});
