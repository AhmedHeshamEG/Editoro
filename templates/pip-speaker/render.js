import { clamp, fitCover, placeholder, roundRectPath } from "/tpl/_shared/kit.js";

const ID = "pip-speaker";

/* How far the lid leans back when it is shut. Not a full 90 degrees: a laptop
   closed flat is invisible, and a window that vanishes to a hairline reads as
   a glitch rather than as a lid. Eighty-two degrees keeps a sliver of screen
   the whole way, which is what makes the motion legible. */
const SHUT_ANGLE = 82 * Math.PI / 180;
/* Viewer distance for the perspective divide, in units of the window height.
   Small numbers exaggerate; large numbers flatten it back into a plain scale.
   Two and a half is about what a laptop on a desk actually subtends. */
const EYE = 2.5;
/* Horizontal strips used to fake the perspective warp. Canvas 2D cannot draw a
   trapezoid, but sixty-four slices of a trapezoid is one, and the seams are
   well under a pixel at any size a PiP window is ever drawn. */
const SLICES = 64;

/* Where the window sits when it is fully open. */
function windowRect(instance, W, H, u) {
  const size = clamp(instance.fields.size ?? 0.26, 0.12, 0.45);
  const w = W * size, h = w * (H / W) * 1.02;
  const margin = 34 * u;
  const corner = instance.fields.corner || "bottom-right";
  const x = corner.endsWith("left") ? margin : W - margin - w;
  const y = corner.startsWith("top") ? margin : H - margin - h;
  return { x, y, w, h, corner };
}

/* How far open the lid is: 0 shut, 1 flat against the screen.

   This reads `p` - the LINEAR progress through whichever phase the block is in
   - and applies its own curve, rather than reusing the motion's opacity. That
   distinction is the whole animation. Opacity has already been through the
   pack's outCubic, so easing it a second time drove the lid to fully open in
   about two frames and the rotation was over before anyone could see it
   happen. Reading `p` puts the hinge on the clock template.json declares.

   The exit runs the same curve backwards, so the lid closes exactly the way it
   opened instead of snapping shut. */
function openness(state) {
  if (state.phase === "in") return easeLid(state.p ?? 1);
  if (state.phase === "out") return easeLid(1 - (state.p ?? 0));
  return 1;
}

/* Smooth, and only smooth. A hinge has friction: it slows into its stop and it
   never springs back past it. The previous version used a back-ease overshoot,
   which is what made the window look like it was being thrown into place
   rather than opened. */
function easeLid(k) {
  return 1 - Math.pow(1 - clamp(k, 0, 1), 4);
}

/* The lid, drawn as a perspective rotation about its hinged edge.

   `k` is arrival: 0 is shut, 1 is open and flat against the screen. Every row
   of the window is a point on a plane hinged along one edge and rotated back
   by `angle`; projecting that plane through a pinhole gives each row a height
   that shrinks with the cosine of the angle and a WIDTH that shrinks with
   distance from the eye. That second part is the whole difference between this
   and a box being squashed - the far edge of a real lid is narrower than the
   near one, and without the taper the eye reads a scale, not a rotation. */
function drawLid(ctx, box, hingeTop, open, drawContents) {
  const angle = (1 - clamp(open, 0, 1)) * SHUT_ANGLE;
  const cos = Math.cos(angle), sin = Math.sin(angle);

  ctx.save();
  // Clip to the projected silhouette so the rounded corners and the taper
  // agree, then let the caller paint inside it.
  const path = new Path2D();
  const edge = v => {
    const depth = v * cos;                     // how far up the screen this row lands
    const persp = EYE / (EYE + v * sin);       // and how wide it is once projected
    return { depth, persp };
  };
  // Walk the two tapered sides to build the silhouette.
  const points = [];
  for (let i = 0; i <= SLICES; i++) {
    const v = i / SLICES, { depth, persp } = edge(v);
    const w = box.w * persp;
    const y = hingeTop ? box.y + depth * box.h : box.y + box.h - depth * box.h;
    points.push({ x: box.x + (box.w - w) / 2, w, y });
  }
  path.moveTo(points[0].x, points[0].y);
  for (const p of points) path.lineTo(p.x, p.y);
  for (let i = points.length - 1; i >= 0; i--) {
    path.lineTo(points[i].x + points[i].w, points[i].y);
  }
  path.closePath();
  ctx.clip(path);

  // Draw the contents strip by strip, each one scaled to its own row width.
  // At k=1 the angle is zero, every persp is 1 and every depth is v, so this
  // collapses to exactly one undistorted drawImage worth of geometry.
  const srcHeight = box.h / SLICES;
  for (let i = 0; i < SLICES; i++) {
    const v0 = i / SLICES, v1 = (i + 1) / SLICES;
    const a = edge(v0), b = edge(v1);
    const yA = hingeTop ? box.y + a.depth * box.h : box.y + box.h - a.depth * box.h;
    const yB = hingeTop ? box.y + b.depth * box.h : box.y + box.h - b.depth * box.h;
    const top = Math.min(yA, yB), dstHeight = Math.abs(yB - yA);
    if (dstHeight <= 0) continue;
    const persp = (a.persp + b.persp) / 2;
    const w = box.w * persp;
    ctx.save();
    ctx.beginPath();
    // The clip is grown by half a pixel at each end so neighbouring strips
    // overlap instead of leaving a hairline of background between them. The
    // scale below uses the true heights, so the overlap costs no distortion.
    ctx.rect(box.x + (box.w - w) / 2, top - 0.5, w, dstHeight + 1);
    ctx.clip();
    // Map this strip back to where it lives on the undistorted window, so the
    // picture stretches with the geometry instead of sliding inside it.
    const srcTop = hingeTop ? box.y + v0 * box.h : box.y + box.h - v1 * box.h;
    ctx.translate(box.x + box.w / 2, top);
    ctx.scale(persp, dstHeight / srcHeight);
    ctx.translate(-(box.x + box.w / 2), -srcTop);
    drawContents(ctx);
    ctx.restore();
  }
  ctx.restore();
}

/* The speaker, cropped out of the source frame and scaled into the window.
   This is the whole point of the rebuild: the old version cut a hole in the
   full-frame asset and let the untouched footage show through it, so the window
   showed whatever was in that corner of the shot - a shoulder, half a head -
   rather than the person. */
function speakerPainter(A, instance, box) {
  const source = A.sourceFrame();
  return ctx => {
    if (source && (source.videoWidth || source.naturalWidth)) {
      const region = A.speakerRegion(instance);
      const sw = source.videoWidth || source.naturalWidth;
      const sh = source.videoHeight || source.naturalHeight;
      const cropW = Math.max(1, region.w * sw), cropH = Math.max(1, region.h * sh);
      const fit = fitCover(cropW, cropH, box.w, box.h);
      ctx.drawImage(
        source,
        region.x * sw, region.y * sh, cropW, cropH,
        box.x + (box.w - fit.w) / 2, box.y + (box.h - fit.h) / 2, fit.w, fit.h,
      );
    } else {
      ctx.fillStyle = "#14161c";
      ctx.fillRect(box.x, box.y, box.w, box.h);
      ctx.save();
      ctx.translate(box.x + box.w / 2, box.y + box.h / 2);
      placeholder(ctx, A, { w: box.w * .7, h: box.h * .5, label: "SPEAKER" });
      ctx.restore();
    }
  };
}

/* The geometry of the entrances that are not the lid. Each resolves to the
   open rectangle at k=1, so the window ends up in exactly the same place
   however it got there. */
function geometry(kind, open, rect, W) {
  const e = clamp(open, 0, 1);
  if (kind === "pop") {
    // Scales up from a little under full size. No overshoot, no bounce.
    const s = 0.88 + 0.12 * e;
    const w = rect.w * s, h = rect.h * s;
    return { x: rect.x + (rect.w - w) / 2, y: rect.y + (rect.h - h) / 2, w, h };
  }
  if (kind === "slide") {
    const dx = rect.corner.endsWith("left") ? -(rect.w + rect.x) : (W - rect.x);
    return { x: rect.x + dx * (1 - e), y: rect.y, w: rect.w, h: rect.h };
  }
  if (kind === "whip") {
    const dx = (rect.corner.endsWith("left") ? -1 : 1) * rect.w * 1.6 * (1 - e);
    return { x: rect.x + dx, y: rect.y, w: rect.w, h: rect.h };
  }
  return { x: rect.x, y: rect.y, w: rect.w, h: rect.h };
}

/* Decoration around the window. Both settle to nothing once the window has
   landed, so a PiP that stays up for ten seconds is not still pulsing at the
   viewer for all ten of them. */
function drawEffects(ctx, kind, box, radius, k, u) {
  if (kind === "none" || k <= 0) return;
  const settle = clamp((1 - k) / .55, 0, 1);
  if (kind === "glow") {
    ctx.save();
    ctx.shadowColor = `rgba(120,220,255,${(.14 + .30 * settle) * k})`;
    ctx.shadowBlur = (22 + 30 * settle) * u;
    roundRectPath(ctx, box.x, box.y, box.w, box.h, radius);
    ctx.strokeStyle = `rgba(160,235,255,${.26 * k})`;
    ctx.lineWidth = Math.max(1, 2.5 * u);
    ctx.stroke();
    ctx.restore();
    return;
  }
  if (settle <= 0) return;
  const spread = (1 - settle) * 34 * u;
  ctx.save();
  ctx.globalAlpha = settle * settle * k;
  roundRectPath(ctx, box.x - spread, box.y - spread,
    box.w + spread * 2, box.h + spread * 2, radius + spread);
  ctx.strokeStyle = "#bfefff";
  ctx.lineWidth = Math.max(1, 2.5 * u);
  ctx.stroke();
  ctx.restore();
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const state = A.motion(instance, t);
    const k = clamp(state.opacity ?? 1, 0, 1);   // fade
    const open = openness(state);                // hinge
    const rect = windowRect(instance, W, H, u);
    const clipUrl = instance.fields.clip ? A.projectAsset(instance.fields.clip) : "";
    const media = clipUrl ? A.video(clipUrl)
      : (instance.fields.asset ? A.load(A.projectAsset(instance.fields.asset)) : null);
    const width = media ? (media.naturalWidth || media.videoWidth || 0) : 0;
    const height = media ? (media.naturalHeight || media.videoHeight || 0) : 0;

    ctx.save();
    ctx.globalAlpha = state.opacity;
    // The full-frame plate first, exactly as before.
    ctx.fillStyle = "#07080b";
    ctx.fillRect(0, 0, W, H);
    if (width && height) {
      const fitted = fitCover(width, height, W, H);
      ctx.drawImage(media, (W - fitted.w) / 2, (H - fitted.h) / 2, fitted.w, fitted.h);
    } else {
      ctx.save();
      ctx.translate(W / 2, H / 2);
      placeholder(ctx, A, { w: W * 0.5, h: H * 0.4, label: "FULL FRAME" });
      ctx.restore();
    }

    const entrance = instance.fields.entrance || "lid";
    const box = geometry(entrance, open, rect, W);
    if (box.w < 1 || box.h < 1) { ctx.restore(); return; }
    const radius = Math.min(18 * u, box.h / 2, box.w / 2);
    const hingeTop = rect.corner.startsWith("top");
    const paint = speakerPainter(A, instance, box);

    drawEffects(ctx, instance.fields.effects || "glow", box, radius, k, u);
    // A drop shadow under the window is what stops it reading as a hole cut in
    // the plate. It tracks the lid: a shut lid casts almost nothing, an open
    // one casts fully, which is most of what sells the rotation as a rotation.
    ctx.save();
    ctx.shadowColor = `rgba(0,0,0,${.55 * k * open})`;
    ctx.shadowBlur = (10 + 20 * open) * u;
    ctx.shadowOffsetY = (3 + 7 * open) * u;
    ctx.fillStyle = "#000";
    roundRectPath(ctx, box.x, box.y + (1 - open) * box.h * (hingeTop ? 0 : 1),
      box.w, Math.max(1, box.h * open), radius);
    ctx.fill();
    ctx.restore();

    if (entrance === "lid" && open < 1) {
      drawLid(ctx, box, hingeTop, open, paint);
      // A lid catches the light as it swings up. The sheen fades out as it
      // reaches its stop, so nothing is left glowing on a settled window.
      const sheen = (1 - open) * 0.5;
      if (sheen > 0.01) {
        ctx.save();
        ctx.globalAlpha = sheen * k;
        ctx.fillStyle = "#dff2ff";
        ctx.globalCompositeOperation = "overlay";
        ctx.fillRect(box.x, box.y, box.w, box.h * open);
        ctx.restore();
      }
    } else {
      ctx.save();
      roundRectPath(ctx, box.x, box.y, box.w, box.h, radius);
      ctx.clip();
      paint(ctx);
      ctx.restore();
    }

    // The bezel, drawn on the projected silhouette so it tapers with the lid.
    ctx.save();
    ctx.globalAlpha = state.opacity * (0.35 + 0.65 * open);
    roundRectPath(ctx, box.x, box.y + (1 - open) * box.h * (hingeTop ? 0 : 1),
      box.w, Math.max(1, box.h * open), radius);
    ctx.strokeStyle = "#ffffff3a";
    ctx.lineWidth = Math.max(1, 3 * u);
    ctx.stroke();
    ctx.restore();
    ctx.restore();
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
