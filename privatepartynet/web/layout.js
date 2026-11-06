/*
 * Tile layout for a voice channel's screens: where each screen sits in the stage, and how a drag or a resize snaps.
 *
 * Pure functions on plain rectangles { x, y, w, h } and a stage { w, h }, with no page in sight, so the tests can run
 * them in Node. The page (room.js) keeps the state and applies the results.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PrivatePartyNetLayout = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var GAP = 10; // between tiles, and between a tile and the stage edge, when tiles are arranged for you
  var PAD = 10; // the margin a tile snaps to along the stage edge
  var SNAP = 10; // how close an edge has to come before it snaps
  var MIN_W = 180;
  var MIN_H = 102;

  function aspectOf(item) {
    return item && item.width > 0 && item.height > 0 ? item.width / item.height : 16 / 9;
  }

  function rounded(key, r) {
    return { key: key, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h) };
  }

  // The largest picture of this item's shape that fits a cell, and no larger than the item's own cap if it has one.
  function fit(item, cellW, cellH) {
    var aspect = item.aspect || aspectOf(item);
    var w = Math.min(cellW, cellH * aspect);
    if (item.maxW) w = Math.min(w, item.maxW);
    if (item.maxH) w = Math.min(w, item.maxH * aspect);
    return { w: w, h: w / aspect };
  }

  // Lay items out in a grid that makes the pictures as large as it can, each row centered. items: [{ key, aspect,
  // maxW?, maxH? }]. Returns [{ key, x, y, w, h }] in the same order.
  function autoArrange(items, stage, options) {
    var gap = options && options.gap != null ? options.gap : GAP;
    var n = items.length;
    if (!n) return [];
    var best = null;
    for (var cols = 1; cols <= n; cols += 1) {
      var rows = Math.ceil(n / cols);
      var cellW = (stage.w - gap * (cols + 1)) / cols;
      var cellH = (stage.h - gap * (rows + 1)) / rows;
      if (cellW < 40 || cellH < 40) continue;
      var sizes = items.map(function (item) { return fit(item, cellW, cellH); });
      var area = sizes.reduce(function (sum, s) { return sum + s.w * s.h; }, 0);
      if (!best || area > best.area + 1) best = { cols: cols, rows: rows, cellH: cellH, sizes: sizes, area: area };
    }
    if (!best) {
      // a stage too small for any grid: one column at the smallest size, which scrolls or overlaps rather than vanishing
      best = {
        cols: 1, rows: n, cellH: MIN_H,
        sizes: items.map(function (item) { return { w: MIN_W, h: MIN_W / (item.aspect || aspectOf(item)) }; }),
      };
    }
    var out = [];
    var index = 0;
    for (var row = 0; row < best.rows; row += 1) {
      var inRow = Math.min(best.cols, n - row * best.cols);
      var rowSizes = best.sizes.slice(index, index + inRow);
      var total = rowSizes.reduce(function (sum, s) { return sum + s.w; }, 0) + gap * (inRow - 1);
      var x = Math.max(0, (stage.w - total) / 2);
      var cellTop = gap + row * (best.cellH + gap);
      for (var i = 0; i < inRow; i += 1) {
        var s = rowSizes[i];
        out.push(rounded(items[index + i].key, { x: x, y: cellTop + (best.cellH - s.h) / 2, w: s.w, h: s.h }));
        x += s.w + gap;
      }
      index += inRow;
    }
    return out;
  }

  function overlaps(a, b, gap) {
    return a.x < b.x + b.w + gap && a.x + a.w + gap > b.x && a.y < b.y + b.h + gap && a.y + a.h + gap > b.y;
  }

  // Keep a rectangle inside the stage: no smaller than the minimum, no larger than the stage, moved back in if it hangs out.
  function clampInto(rect, stage, minW, minH) {
    var w = Math.max(Math.min(minW || MIN_W, stage.w), Math.min(rect.w, stage.w));
    var h = Math.max(Math.min(minH || MIN_H, stage.h), Math.min(rect.h, stage.h));
    return {
      x: Math.min(Math.max(0, rect.x), Math.max(0, stage.w - w)),
      y: Math.min(Math.max(0, rect.y), Math.max(0, stage.h - h)),
      w: w,
      h: h,
    };
  }

  // The size a new tile gets when others are already placed by hand: about a third of the stage, in the picture's shape.
  function defaultSize(aspect, stage) {
    var w = Math.max(MIN_W, Math.min(640, stage.w * 0.36));
    var h = w / aspect;
    if (h > stage.h * 0.6) {
      h = stage.h * 0.6;
      w = h * aspect;
    }
    return { w: Math.round(Math.max(MIN_W, w)), h: Math.round(Math.max(MIN_H, h)) };
  }

  // The first spot a tile of this size fits without touching another, scanning from the top left. With no room left it
  // steps down and across the existing tiles instead, so a new tile is never hidden under the stage edge.
  function findFreeSpot(size, rects, stage, gap) {
    var g = gap == null ? GAP : gap;
    var w = Math.min(size.w, Math.max(MIN_W, stage.w - 2 * g));
    var h = Math.min(size.h, Math.max(MIN_H, stage.h - 2 * g));
    for (var y = g; y + h <= stage.h - g + 0.5; y += 20) {
      for (var x = g; x + w <= stage.w - g + 0.5; x += 20) {
        var candidate = { x: x, y: y, w: w, h: h };
        var blocked = rects.some(function (r) { return overlaps(candidate, r, g - 1); });
        if (!blocked) return candidate;
      }
    }
    var offset = (rects.length % 8) * 28;
    return clampInto({ x: g + offset, y: g + offset, w: w, h: h }, stage);
  }

  // The closest target to a value, if one is within the snapping distance.
  function nearest(value, targets) {
    var best = null;
    targets.forEach(function (target) {
      var delta = target.pos - value;
      if (Math.abs(delta) <= SNAP && (!best || Math.abs(delta) < Math.abs(best.delta))) best = { delta: delta, target: target };
    });
    return best;
  }

  // Lines a left or top edge (lo), a right or bottom edge (hi) or a middle can snap to, along one axis.
  function linesFor(others, stageSize, axis) {
    var pos = axis === "x" ? "x" : "y";
    var len = axis === "x" ? "w" : "h";
    var lines = [
      { pos: PAD, edge: "lo", ref: null },
      { pos: stageSize - PAD, edge: "hi", ref: null },
      { pos: stageSize / 2, edge: "mid", ref: null },
    ];
    others.forEach(function (o) {
      lines.push({ pos: o[pos], edge: "lo", ref: o }); // line up with the other's near edge
      lines.push({ pos: o[pos] + o[len] + GAP, edge: "lo", ref: o }); // sit just past the other
      lines.push({ pos: o[pos] + o[len], edge: "hi", ref: o }); // line up with the other's far edge
      lines.push({ pos: o[pos] - GAP, edge: "hi", ref: o }); // sit just before the other
      lines.push({ pos: o[pos] + o[len] / 2, edge: "mid", ref: o });
    });
    return lines;
  }

  function guideFor(axis, pos, rect, ref) {
    if (axis === "x") {
      var top = ref ? Math.min(rect.y, ref.y) : 0;
      var bottom = ref ? Math.max(rect.y + rect.h, ref.y + ref.h) : null;
      return { axis: "v", pos: pos, from: top, to: bottom };
    }
    var left = ref ? Math.min(rect.x, ref.x) : 0;
    var right = ref ? Math.max(rect.x + rect.w, ref.x + ref.w) : null;
    return { axis: "h", pos: pos, from: left, to: right };
  }

  // Snap a rectangle that is being dragged. Its left, middle and right line up with the stage edges, the stage middle,
  // and the edges and middles of the other tiles; the same for top, middle and bottom. Returns the new position and the
  // guides to draw. A guide with a null end runs to the edge of the stage.
  function snapMove(rect, others, stage) {
    var out = { x: rect.x, y: rect.y, w: rect.w, h: rect.h };
    var guides = [];
    var xLines = linesFor(others, stage.w, "x");
    var yLines = linesFor(others, stage.h, "y");
    var bestX = null;
    xLines.forEach(function (line) {
      var edgeValue = line.edge === "lo" ? rect.x : line.edge === "hi" ? rect.x + rect.w : rect.x + rect.w / 2;
      var delta = line.pos - edgeValue;
      if (Math.abs(delta) <= SNAP && (!bestX || Math.abs(delta) < Math.abs(bestX.delta))) bestX = { delta: delta, line: line };
    });
    if (bestX) out.x += bestX.delta;
    var bestY = null;
    yLines.forEach(function (line) {
      var edgeValue = line.edge === "lo" ? rect.y : line.edge === "hi" ? rect.y + rect.h : rect.y + rect.h / 2;
      var delta = line.pos - edgeValue;
      if (Math.abs(delta) <= SNAP && (!bestY || Math.abs(delta) < Math.abs(bestY.delta))) bestY = { delta: delta, line: line };
    });
    if (bestY) out.y += bestY.delta;
    out = clampInto(out, stage, rect.w, rect.h); // moving never changes the size
    if (bestX) {
      var gx = bestX.line.edge === "lo" ? out.x : bestX.line.edge === "hi" ? out.x + out.w : out.x + out.w / 2;
      guides.push(guideFor("x", gx, out, bestX.line.ref));
    }
    if (bestY) {
      var gy = bestY.line.edge === "lo" ? out.y : bestY.line.edge === "hi" ? out.y + out.h : out.y + out.h / 2;
      guides.push(guideFor("y", gy, out, bestY.line.ref));
    }
    return { x: out.x, y: out.y, guides: guides };
  }

  // Resize a rectangle by dragging one of its edges or corners. edge is "n", "s", "e", "w", "ne", "nw", "se" or "sw";
  // r0 is the rectangle when the drag began and dx, dy how far the pointer has moved. The moving edges snap like a drag
  // does, and a tile that comes within snapping distance of another tile's width or height takes that size. With
  // options.aspect set the picture keeps its shape (hold Shift in the page to resize freely). Returns { rect, guides }.
  function resize(r0, edge, dx, dy, others, stage, options) {
    var opts = options || {};
    var aspect = opts.aspect || 0;
    var minW = opts.minW || MIN_W;
    var minH = opts.minH || MIN_H;
    var west = edge.indexOf("w") >= 0;
    var east = edge.indexOf("e") >= 0;
    var north = edge.indexOf("n") >= 0;
    var south = edge.indexOf("s") >= 0;
    var x1 = r0.x;
    var y1 = r0.y;
    var x2 = r0.x + r0.w;
    var y2 = r0.y + r0.h;
    if (east) x2 += dx;
    if (west) x1 += dx;
    if (south) y2 += dy;
    if (north) y1 += dy;

    var driver = null;
    if (aspect) {
      if ((west || east) && !(north || south)) driver = "w";
      else if ((north || south) && !(west || east)) driver = "h";
      else driver = (x2 - x1) / r0.w >= (y2 - y1) / r0.h ? "w" : "h";
    }

    var guides = [];
    var xLines = linesFor(others, stage.w, "x");
    var yLines = linesFor(others, stage.h, "y");
    var rectNow = function () { return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 }; };
    var snappedX = false;
    var snappedY = false;

    if (!aspect || driver === "w") {
      if (east) {
        var se = nearest(x2, xLines.filter(function (l) { return l.edge === "hi"; }));
        if (se) { x2 += se.delta; snappedX = true; guides.push(guideFor("x", x2, rectNow(), se.target.ref)); }
      }
      if (west) {
        var sw = nearest(x1, xLines.filter(function (l) { return l.edge === "lo"; }));
        if (sw) { x1 += sw.delta; snappedX = true; guides.push(guideFor("x", x1, rectNow(), sw.target.ref)); }
      }
    }
    if (!aspect || driver === "h") {
      if (south) {
        var ss = nearest(y2, yLines.filter(function (l) { return l.edge === "hi"; }));
        if (ss) { y2 += ss.delta; snappedY = true; guides.push(guideFor("y", y2, rectNow(), ss.target.ref)); }
      }
      if (north) {
        var sn = nearest(y1, yLines.filter(function (l) { return l.edge === "lo"; }));
        if (sn) { y1 += sn.delta; snappedY = true; guides.push(guideFor("y", y1, rectNow(), sn.target.ref)); }
      }
    }

    // take another tile's size when close to it and nothing else has snapped on that axis
    if ((west || east) && !snappedX && (!aspect || driver === "w")) {
      var wantedW = nearest(x2 - x1, others.map(function (o) { return { pos: o.w, ref: o }; }));
      if (wantedW) {
        if (west && !east) x1 = x2 - (x2 - x1 + wantedW.delta);
        else x2 = x1 + (x2 - x1 + wantedW.delta);
      }
    }
    if ((north || south) && !snappedY && (!aspect || driver === "h")) {
      var wantedH = nearest(y2 - y1, others.map(function (o) { return { pos: o.h, ref: o }; }));
      if (wantedH) {
        if (north && !south) y1 = y2 - (y2 - y1 + wantedH.delta);
        else y2 = y1 + (y2 - y1 + wantedH.delta);
      }
    }

    if (x2 - x1 < minW) {
      if (west && !east) x1 = x2 - minW;
      else x2 = x1 + minW;
    }
    if (y2 - y1 < minH) {
      if (north && !south) y1 = y2 - minH;
      else y2 = y1 + minH;
    }

    if (aspect) {
      if (driver === "w") {
        var h = (x2 - x1) / aspect;
        if (north) y1 = y2 - h;
        else y2 = y1 + h;
      } else {
        var w = (y2 - y1) * aspect;
        if (west) x1 = x2 - w;
        else x2 = x1 + w;
      }
    }

    // stay inside the stage
    if (aspect) {
      // keep the shape: scale about the corner that is not being dragged until it fits the room left both ways
      var availW = west ? x2 : stage.w - x1;
      var availH = north ? y2 : stage.h - y1;
      var scale = Math.min(1, availW / (x2 - x1), availH / (y2 - y1));
      if (scale < 1) {
        var newW = (x2 - x1) * scale;
        var newH = (y2 - y1) * scale;
        if (west) x1 = x2 - newW;
        else x2 = x1 + newW;
        if (north) y1 = y2 - newH;
        else y2 = y1 + newH;
      }
    } else {
      x1 = Math.max(0, x1);
      y1 = Math.max(0, y1);
      x2 = Math.min(stage.w, x2);
      y2 = Math.min(stage.h, y2);
    }

    return { rect: { x: x1, y: y1, w: x2 - x1, h: y2 - y1 }, guides: guides };
  }

  return {
    GAP: GAP, PAD: PAD, SNAP: SNAP, MIN_W: MIN_W, MIN_H: MIN_H,
    aspectOf: aspectOf,
    autoArrange: autoArrange,
    findFreeSpot: findFreeSpot,
    defaultSize: defaultSize,
    clampInto: clampInto,
    snapMove: snapMove,
    resize: resize,
    overlaps: overlaps,
  };
});
