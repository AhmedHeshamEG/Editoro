/* ===========================================================================
   Editoro shared render kit  —  templates/_shared/kit.js

   Every template pack imports from here. The kit owns the *look* (paper, ink,
   shadows, hand-drawn marks, text layout); the motion engine in index.html owns
   the *timing*. A pack's render.js should read as a description of one visual,
   not as a pile of canvas boilerplate.

   All drawing is in "design units": 1 unit ≈ 1 px at 1080p, scaled by A.unit(ctx)
   so a template looks identical at 720p, 1080p and 4K, and in both orientations.

   Coordinates are centred on the instance origin — the caller has already
   translated the context to (inst.x * W, inst.y * H).
   =========================================================================== */

export const SHARED = "/tpl/_shared";
export const art = name => `${SHARED}/art/${name}`;

export const PALETTE = {
  ink: "#1b1b1e",
  inkSoft: "#4a4a52",
  inkFaint: "#8a8a94",
  rule: "#8aa7c6",
  paper: "#f7f2e4",
  paperWhite: "#fcfbf8",
  kraft: "#cbb28a",
  note: "#fce48a",
  dark: "#202228",
  darkLine: "#3a3d46",
  accent: "#ffd166",
  danger: "#ff6b6b",
  good: "#7dd3a0",
  shadow: "#000000",
};

const RTL = /[֐-׿؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]/;

/** Arabic and Hebrew need right alignment and RTL bidi ordering. */
export const isRTL = text => RTL.test(String(text || ""));

/** Apply the writing direction implied by the text itself. */
export function applyDirection(ctx, text) {
  ctx.direction = isRTL(text) ? "rtl" : "ltr";
  return ctx.direction;
}

// --------------------------------------------------------------- geometry
export function roundRectPath(ctx, x, y, w, h, r) {
  const radius = Math.max(0, Math.min(r, Math.min(w, h) / 2));
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, radius);
}

export function centeredRect(w, h) {
  return { x: -w / 2, y: -h / 2, w, h };
}

export function fitContain(iw, ih, maxW, maxH) {
  const k = Math.min(maxW / Math.max(1, iw), maxH / Math.max(1, ih));
  return { w: iw * k, h: ih * k, k };
}

export function fitCover(iw, ih, maxW, maxH) {
  const k = Math.max(maxW / Math.max(1, iw), maxH / Math.max(1, ih));
  return { w: iw * k, h: ih * k, k };
}

export const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
export const mix = (a, b, k) => a + (b - a) * k;

/** "#rrggbb" + alpha 0..1 -> "rgba(...)"; passes through anything else. */
export function alpha(color, a) {
  const hex = String(color || "").trim();
  if (!/^#[0-9a-f]{6}$/i.test(hex)) return hex;
  const value = parseInt(hex.slice(1), 16);
  return `rgba(${value >> 16 & 255},${value >> 8 & 255},${value & 255},${clamp(a, 0, 1)})`;
}

// ------------------------------------------------------------ text layout
/**
 * Wrap `text` to at most `maxLines`, ellipsising the last line if it overflows.
 * The caller must have set ctx.font already.
 */
export function wrapLines(ctx, text, maxWidth, maxLines = 2) {
  const source = String(text ?? "").trim();
  if (!source) return [""];
  const explicit = source.split("\n");
  const lines = [];
  for (const paragraph of explicit) {
    const words = paragraph.split(/\s+/).filter(Boolean);
    let line = "";
    for (const word of words) {
      const candidate = line ? `${line} ${word}` : word;
      if (line && ctx.measureText(candidate).width > maxWidth) {
        lines.push(line);
        line = word;
        if (lines.length >= maxLines) break;
      } else {
        line = candidate;
      }
    }
    if (line && lines.length < maxLines) lines.push(line);
    if (lines.length >= maxLines) break;
  }
  if (!lines.length) lines.push(source.slice(0, 40));
  const consumed = lines.join(" ").length;
  if (consumed < source.replace(/\s+/g, " ").length) {
    let last = lines[lines.length - 1];
    while (last.length > 1 && ctx.measureText(`${last}…`).width > maxWidth) last = last.slice(0, -1);
    lines[lines.length - 1] = `${last}…`;
  }
  return lines.slice(0, maxLines);
}

/**
 * Pick the largest font size that fits `text` into maxWidth × maxLines.
 * This is what keeps a template from breaking when the text is longer than the
 * designer imagined — the card grows a little, the type shrinks a little, and
 * nothing ever overflows the frame.
 */
export function fitText(ctx, text, {
  maxWidth, maxLines = 2, weight = 700, family = "Rubik",
  size, minSize = 12, tracking = 0,
}) {
  let current = size;
  for (let attempt = 0; attempt < 24; attempt++) {
    ctx.font = `${weight} ${current}px ${family}`;
    ctx.letterSpacing = `${tracking}px`;
    const lines = wrapLines(ctx, text, maxWidth, maxLines);
    const widest = Math.max(...lines.map(line => ctx.measureText(line).width));
    const overflows = lines.some(line => line.endsWith("…")) && lines.length >= maxLines;
    if ((widest <= maxWidth && !overflows) || current <= minSize) {
      return { size: current, lines, width: Math.min(widest, maxWidth), font: ctx.font };
    }
    current = Math.max(minSize, current * 0.94);
  }
  const lines = wrapLines(ctx, text, maxWidth, maxLines);
  return { size: current, lines, width: maxWidth, font: ctx.font };
}

/** Draw pre-wrapped lines centred on (0, 0) unless an origin is given. */
export function drawLines(ctx, lines, {
  size, lineHeight = 1.12, color = PALETTE.ink, align = "center",
  x = 0, y = 0, weight = 700, family = "Rubik", tracking = 0,
  stroke = null, strokeWidth = 0,
}) {
  ctx.save();
  ctx.font = `${weight} ${size}px ${family}`;
  ctx.letterSpacing = `${tracking}px`;
  ctx.textAlign = align;
  ctx.textBaseline = "middle";
  const step = size * lineHeight;
  lines.forEach((line, index) => {
    applyDirection(ctx, line);
    const lineY = y + (index - (lines.length - 1) / 2) * step;
    if (stroke && strokeWidth > 0) {
      ctx.lineJoin = "round";
      ctx.lineWidth = strokeWidth;
      ctx.strokeStyle = stroke;
      ctx.strokeText(line, x, lineY);
    }
    ctx.fillStyle = color;
    ctx.fillText(line, x, lineY);
  });
  ctx.restore();
  return { height: lines.length * step, step };
}

// ------------------------------------------------------------- surfaces
/**
 * The house paper card. Texture, a warm edge, ruled lines when asked, and a
 * lifted top edge so it reads as a real sheet resting above the frame rather
 * than a rectangle pasted onto it.
 */
export function paperCard(ctx, A, {
  w, h, radius = 10, seed = "card", texture = "paper-cream.png",
  tint = null, rule = false, ruleColor = PALETTE.rule, lift = true,
  border = "#00000022", opacity = 1,
}) {
  const u = A.unit(ctx);
  const image = A.load(art(texture));
  ctx.save();
  ctx.globalAlpha *= opacity;
  roundRectPath(ctx, -w / 2, -h / 2, w, h, radius * u);
  if (image?.complete && image.naturalWidth) {
    const pattern = ctx.createPattern(image, "repeat");
    // The texture is authored at 512 px. Scaling the pattern with the render
    // unit keeps the paper grain the same apparent size whether this frame is
    // a 720p preview or a 4K export — otherwise the grain visibly changes
    // between what you approve in the preview and what lands in the file.
    if (pattern?.setTransform) {
      const scale = Math.max(0.35, u * 0.55);
      pattern.setTransform(new DOMMatrix([scale, 0, 0, scale, -w / 2, -h / 2]));
    }
    ctx.fillStyle = pattern || PALETTE.paper;
  } else {
    ctx.fillStyle = PALETTE.paper;
  }
  ctx.fill();
  if (tint) {
    ctx.fillStyle = tint;
    ctx.fill();
  }
  if (rule) {
    const random = A.seeded(`${seed}-rule`);
    ctx.save();
    ctx.clip();
    ctx.strokeStyle = alpha(ruleColor, 0.22);
    ctx.lineWidth = Math.max(0.6, u);
    const spacing = (typeof rule === "number" ? rule : 17) * u;
    for (let y = -h / 2 + spacing; y < h / 2 - spacing * 0.3; y += spacing) {
      ctx.beginPath();
      ctx.moveTo(-w / 2 + 9 * u, y + (random() - 0.5) * u);
      ctx.lineTo(w / 2 - 9 * u, y + (random() - 0.5) * u);
      ctx.stroke();
    }
    ctx.restore();
  }
  if (lift) {
    // A one-pixel warm highlight along the top edge and a cool line along the
    // bottom: the cheapest honest cue that the sheet has thickness.
    ctx.save();
    ctx.clip();
    ctx.strokeStyle = "#ffffff70";
    ctx.lineWidth = Math.max(1, 1.4 * u);
    ctx.beginPath();
    ctx.moveTo(-w / 2 + radius * u, -h / 2 + 0.7 * u);
    ctx.lineTo(w / 2 - radius * u, -h / 2 + 0.7 * u);
    ctx.stroke();
    ctx.strokeStyle = "#00000018";
    ctx.beginPath();
    ctx.moveTo(-w / 2 + radius * u, h / 2 - 0.7 * u);
    ctx.lineTo(w / 2 - radius * u, h / 2 - 0.7 * u);
    ctx.stroke();
    ctx.restore();
  }
  if (border) {
    roundRectPath(ctx, -w / 2, -h / 2, w, h, radius * u);
    ctx.strokeStyle = border;
    ctx.lineWidth = Math.max(0.6, u);
    ctx.stroke();
  }
  ctx.restore();
}

/** A dark UI surface for code, screens and chat — the counterpart to paper. */
export function darkPanel(ctx, A, { w, h, radius = 14, fill = "#22242b", border = "#ffffff14" }) {
  const u = A.unit(ctx);
  roundRectPath(ctx, -w / 2, -h / 2, w, h, radius * u);
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.strokeStyle = border;
  ctx.lineWidth = Math.max(1, u);
  ctx.stroke();
}

/** Translucent washi tape, for pinning cards to the frame. */
export function tapeStrip(ctx, A, { x = 0, y = 0, w = 120, angle = -0.12, opacity = 0.85 }) {
  const u = A.unit(ctx);
  const image = A.load(art("tape.png"));
  if (!(image?.complete && image.naturalWidth)) return;
  const height = w * (image.naturalHeight / image.naturalWidth);
  ctx.save();
  ctx.globalAlpha *= opacity;
  ctx.translate(x, y);
  ctx.rotate(angle);
  ctx.drawImage(image, (-w / 2) * u, (-height / 2) * u, w * u, height * u);
  ctx.restore();
}

/** Film grain over a region — used sparingly, to stop flat fills looking digital. */
export function grain(ctx, A, { w, h, opacity = 0.05 }) {
  const image = A.load(art("grain.png"));
  if (!(image?.complete && image.naturalWidth)) return;
  ctx.save();
  ctx.globalAlpha *= opacity;
  ctx.globalCompositeOperation = "overlay";
  const pattern = ctx.createPattern(image, "repeat");
  if (pattern) {
    ctx.fillStyle = pattern;
    ctx.fillRect(-w / 2, -h / 2, w, h);
  }
  ctx.restore();
}

// -------------------------------------------------------- hand-drawn marks
/**
 * A marker sweep. `progress` 0→1 draws it left to right, with the ink lagging
 * slightly behind the nib the way a real highlighter does.
 */
export function highlighterSweep(ctx, A, {
  x, y, w, h, progress = 1, color = null, opacity = 0.85,
}) {
  const p = clamp(progress, 0, 1);
  if (p <= 0) return;
  const image = A.load(art("highlighter.png"));
  ctx.save();
  ctx.globalAlpha *= opacity;
  if (image?.complete && image.naturalWidth) {
    const sliceWidth = Math.max(1, image.naturalWidth * p);
    if (color) {
      // Tint by drawing the stroke as a mask for a flat colour fill.
      ctx.save();
      ctx.beginPath();
      ctx.rect(x, y - h / 2, w * p, h);
      ctx.clip();
      ctx.drawImage(image, 0, 0, sliceWidth, image.naturalHeight, x, y - h / 2, w * p, h);
      ctx.globalCompositeOperation = "source-atop";
      ctx.fillStyle = alpha(color, 0.75);
      ctx.fillRect(x, y - h / 2, w * p, h);
      ctx.restore();
    } else {
      ctx.drawImage(image, 0, 0, sliceWidth, image.naturalHeight, x, y - h / 2, w * p, h);
    }
  } else {
    ctx.strokeStyle = alpha(color || "#f9f871", 0.6);
    ctx.lineWidth = h;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + w * p, y);
    ctx.stroke();
  }
  ctx.restore();
}

/** A hand-drawn circle or ellipse around something, drawn as it is "written". */
export function scribbleEllipse(ctx, {
  w, h, progress = 1, seed = "circle", color = PALETTE.danger,
  width = 6, laps = 1.18,
}) {
  const p = clamp(progress, 0, 1);
  if (p <= 0) return;
  const turns = laps * Math.PI * 2 * p;
  const start = -Math.PI * 0.62;
  const steps = Math.max(12, Math.round(64 * p * laps));
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  for (let step = 0; step <= steps; step++) {
    const angle = start + turns * (step / steps);
    const drift = 1 + 0.035 * Math.sin(angle * 2.7 + seed.length) + 0.02 * Math.sin(angle * 5.1);
    const px = Math.cos(angle) * (w / 2) * drift;
    const py = Math.sin(angle) * (h / 2) * drift;
    if (step === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.stroke();
  ctx.restore();
}

/** A hand-drawn underline that sags slightly in the middle. */
export function scribbleUnderline(ctx, {
  w, progress = 1, color = PALETTE.accent, width = 6, sag = 5, seed = "u",
}) {
  const p = clamp(progress, 0, 1);
  if (p <= 0) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.beginPath();
  const steps = 24;
  for (let step = 0; step <= steps * p; step++) {
    const k = step / steps;
    const px = -w / 2 + w * k;
    const py = Math.sin(k * Math.PI) * sag + Math.sin(k * 9.1 + seed.length) * width * 0.16;
    if (step === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.stroke();
  ctx.restore();
}

/** A pen arrow with a natural bend and a two-stroke head. */
export function handArrow(ctx, {
  x1, y1, x2, y2, bend = 0.22, width = 7, color = PALETTE.danger,
  progress = 1, head = 26,
}) {
  const p = clamp(progress, 0, 1);
  if (p <= 0.01) return;
  const midX = (x1 + x2) / 2 - (y2 - y1) * bend;
  const midY = (y1 + y2) / 2 + (x2 - x1) * bend;
  const at = k => {
    const inverse = 1 - k;
    return {
      x: inverse * inverse * x1 + 2 * inverse * k * midX + k * k * x2,
      y: inverse * inverse * y1 + 2 * inverse * k * midY + k * k * y2,
    };
  };
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  const shaft = clamp(p / 0.82, 0, 1);
  ctx.beginPath();
  for (let step = 0; step <= 40; step++) {
    const k = (step / 40) * shaft;
    const point = at(k);
    if (step === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
  }
  ctx.stroke();
  if (p > 0.82) {
    const tip = at(shaft);
    const before = at(Math.max(0, shaft - 0.06));
    const angle = Math.atan2(tip.y - before.y, tip.x - before.x);
    const headProgress = clamp((p - 0.82) / 0.18, 0, 1) * head;
    for (const spread of [2.5, -2.5]) {
      ctx.beginPath();
      ctx.moveTo(tip.x, tip.y);
      ctx.lineTo(
        tip.x + Math.cos(angle + spread) * headProgress,
        tip.y + Math.sin(angle + spread) * headProgress,
      );
      ctx.stroke();
    }
  }
  ctx.restore();
}

/** A tick drawn stroke-by-stroke, for checklists. */
export function checkMark(ctx, { size = 24, progress = 1, color = PALETTE.good, width = 5 }) {
  const p = clamp(progress, 0, 1);
  if (p <= 0) return;
  const points = [
    { x: -size * 0.42, y: size * 0.05 },
    { x: -size * 0.10, y: size * 0.36 },
    { x: size * 0.46, y: -size * 0.38 },
  ];
  const first = Math.hypot(points[1].x - points[0].x, points[1].y - points[0].y);
  const second = Math.hypot(points[2].x - points[1].x, points[2].y - points[1].y);
  const travelled = (first + second) * p;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  if (travelled <= first) {
    const k = travelled / first;
    ctx.lineTo(mix(points[0].x, points[1].x, k), mix(points[0].y, points[1].y, k));
  } else {
    ctx.lineTo(points[1].x, points[1].y);
    const k = clamp((travelled - first) / second, 0, 1);
    ctx.lineTo(mix(points[1].x, points[2].x, k), mix(points[1].y, points[2].y, k));
  }
  ctx.stroke();
  ctx.restore();
}

/** A small rounded label chip. Returns its measured width. */
export function chip(ctx, A, {
  text, x = 0, y = 0, size = 22, weight = 700, color = PALETTE.ink,
  background = PALETTE.accent, padding = 12, radius = 8, family = "Rubik",
}) {
  const u = A.unit(ctx);
  ctx.save();
  ctx.font = `${weight} ${size}px ${family}`;
  const width = ctx.measureText(text).width + padding * 2 * u;
  const height = size * 1.62;
  roundRectPath(ctx, x - width / 2, y - height / 2, width, height, radius * u);
  ctx.fillStyle = background;
  ctx.fill();
  ctx.fillStyle = color;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  applyDirection(ctx, text);
  ctx.fillText(text, x, y + size * 0.03);
  ctx.restore();
  return { width, height };
}

// ------------------------------------------------------------- utilities
/** Format a number for the stat pack: grouping, decimals, prefix/suffix. */
export function formatNumber(value, { decimals = 0, group = true, prefix = "", suffix = "" } = {}) {
  const numeric = Number.isFinite(value) ? value : 0;
  let body = Math.abs(numeric).toFixed(Math.max(0, Math.min(6, decimals)));
  if (group) {
    const [whole, fraction] = body.split(".");
    body = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fraction ? `.${fraction}` : "");
  }
  return `${numeric < 0 ? "-" : ""}${prefix}${body}${suffix}`;
}

/** Split a "a | b | c" or newline-separated field into trimmed items. */
export function splitItems(text, limit = 8) {
  return String(text ?? "")
    .split(/\r?\n|\s*\|\s*/)
    .map(item => item.trim())
    .filter(Boolean)
    .slice(0, limit);
}

/** Draw an image with a rounded-rect clip, cover-fitted into w × h. */
export function roundedImage(ctx, image, { x, y, w, h, radius = 0 }) {
  if (!(image?.complete && image.naturalWidth)) return false;
  ctx.save();
  if (radius > 0) {
    roundRectPath(ctx, x, y, w, h, radius);
    ctx.clip();
  }
  const fitted = fitCover(image.naturalWidth, image.naturalHeight, w, h);
  ctx.drawImage(image, x + (w - fitted.w) / 2, y + (h - fitted.h) / 2, fitted.w, fitted.h);
  ctx.restore();
  return true;
}

/** The standard "asset not chosen yet" plate, so a block is never invisible. */
export function placeholder(ctx, A, { w, h, label = "ASSET", radius = 8 }) {
  const u = A.unit(ctx);
  ctx.save();
  roundRectPath(ctx, -w / 2, -h / 2, w, h, radius * u);
  ctx.fillStyle = "#00000026";
  ctx.fill();
  ctx.setLineDash([8 * u, 7 * u]);
  ctx.strokeStyle = "#ffffff44";
  ctx.lineWidth = Math.max(1, 2 * u);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#ffffff88";
  ctx.font = `700 ${Math.max(10, Math.min(h * 0.16, 22 * u))}px Inter`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, 0, 0);
  ctx.restore();
}
