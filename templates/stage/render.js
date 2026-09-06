import { PALETTE, alpha, clamp, grain, roundRectPath } from "/tpl/_shared/kit.js";

/* The stage is the only pack that owns the whole frame opaquely.

   Its ground is therefore always fully opaque and `intensity` controls only the
   texture drawn on top. That split is what makes one template cover three
   framings honestly: "behind" needs the ground to hide the room so the matte
   can put the speaker back on top of it, "solo" needs it to hide the shot
   entirely, and "pip" needs it to hide the shot so the window is the only place
   the speaker appears. A semi-transparent ground would leave the old room
   ghosting through all three. */

const ID = "stage";

/* Warm ink rather than paper white. A backdrop sits behind a person lit by a
   room, and a bright ground behind a dim subject reads as a bad key. This is
   the same warm family as the note cards, several stops down. */
const GROUND_TOP = "#26221c";
const GROUND_BOTTOM = "#15130f";

function ground(ctx, W, H) {
  const wash = ctx.createLinearGradient(0, 0, W * 0.25, H);
  wash.addColorStop(0, GROUND_TOP);
  wash.addColorStop(1, GROUND_BOTTOM);
  ctx.fillStyle = wash;
  ctx.fillRect(0, 0, W, H);
}

/* Notebook ruling, drifting upward slowly enough that you never catch a line
   leaving. The margin rule is the detail that stops it reading as a generic
   grid: it is a page, and a page has one red line down the side. */
function ruling(ctx, W, H, seconds, u, strength) {
  const spacing = 46 * u;
  const drift = (seconds * spacing * 0.045) % spacing;
  ctx.lineWidth = Math.max(1, 1.1 * u);
  ctx.strokeStyle = alpha(PALETTE.rule, 0.16 * strength);
  for (let y = -spacing + drift; y < H + spacing; y += spacing) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(W, y);
    ctx.stroke();
  }
  const margin = W * 0.14 + Math.sin(seconds * 0.06) * W * 0.004;
  ctx.strokeStyle = alpha(PALETTE.danger, 0.20 * strength);
  ctx.lineWidth = Math.max(1, 1.6 * u);
  ctx.beginPath();
  ctx.moveTo(margin, 0);
  ctx.lineTo(margin, H);
  ctx.stroke();
}

/* Light moving over a still surface: two pools at different speeds, screened
   on. One pool alone reads as a spotlight sweep, which is a different and much
   busier idea. */
function pools(ctx, W, H, seconds, random, strength, count = 4) {
  ctx.globalCompositeOperation = "screen";
  for (let pool = 0; pool < count; pool++) {
    const phase = random() * 6.283, speed = 0.025 + random() * 0.035;
    const x = W * (0.15 + 0.70 * random()) + Math.sin(seconds * speed + phase) * W * 0.07;
    const y = H * (0.15 + 0.70 * random()) + Math.cos(seconds * speed * 0.8 + phase) * H * 0.07;
    const gradient = ctx.createRadialGradient(x, y, 0, x, y, H * 0.42);
    gradient.addColorStop(0, alpha(PALETTE.kraft, 0.13 * strength));
    gradient.addColorStop(1, alpha(PALETTE.kraft, 0));
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, W, H);
  }
  ctx.globalCompositeOperation = "source-over";
}

/* Torn paper drifting up. Sparse and slow: seven pieces crossing the frame over
   about a minute is texture, twenty is weather. */
function shapes(ctx, W, H, seconds, random, u, strength) {
  for (let piece = 0; piece < 7; piece++) {
    const size = W * (0.05 + random() * 0.09);
    const speed = 0.006 + random() * 0.010;
    const x = W * random();
    const y = H * (1.15 - ((random() + seconds * speed) % 1.3));
    const spin = (random() - 0.5) * 0.5 + Math.sin(seconds * 0.09 + piece) * 0.06;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(spin);
    ctx.fillStyle = alpha(piece % 3 === 0 ? PALETTE.note : PALETTE.kraft, 0.15 * strength);
    roundRectPath(ctx, -size / 2, -size / 2, size, size * 0.72, 4 * u);
    ctx.fill();
    ctx.restore();
  }
}

/* The speaker, cropped to where they actually are in the source frame and set
   on the backdrop as a card. Reuses the project's speaker region rather than
   asking again, for the same reason the PiP window does: a talking head does
   not move between shots. */
function speakerWindow(ctx, instance, A, W, H, u, state) {
  const source = A.sourceFrame();
  const width = W * 0.34, height = width * (H / W) * 1.18;
  const x = W * 0.5 - width / 2, y = H * 0.5 - height / 2;
  A.shadow(ctx, state.shadowSpec, c => roundRectPath(c, x, y, width, height, 18 * u));
  ctx.save();
  roundRectPath(ctx, x, y, width, height, 18 * u);
  ctx.fillStyle = PALETTE.dark;
  ctx.fill();
  ctx.clip();
  if (source) {
    const region = A.speakerRegion(instance);
    const sw = source.videoWidth || source.width, sh = source.videoHeight || source.height;
    const cropX = region.x * sw, cropY = region.y * sh;
    const cropW = Math.max(1, region.w * sw), cropH = Math.max(1, region.h * sh);
    // Cover, so the window is always full: the region's aspect and the
    // window's rarely agree and letterboxing a face is worse than cropping it.
    const scale = Math.max(width / cropW, height / cropH);
    const drawW = cropW * scale, drawH = cropH * scale;
    ctx.drawImage(source, cropX, cropY, cropW, cropH,
      x + (width - drawW) / 2, y + (height - drawH) / 2, drawW, drawH);
  }
  ctx.restore();
  ctx.strokeStyle = alpha(PALETTE.paper, 0.30);
  ctx.lineWidth = Math.max(1, 2 * u);
  roundRectPath(ctx, x, y, width, height, 18 * u);
  ctx.stroke();
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const state = A.motion(instance, t);
    const u = A.unit(ctx);
    const random = A.seeded(instance.id);
    const seconds = state.seconds;
    const strength = clamp(instance.fields.intensity ?? 0.6, 0.15, 1);
    const style = instance.fields.style || "grid";

    ctx.save();
    ctx.globalAlpha = state.opacity;
    ground(ctx, W, H);
    if (style === "grid") { ruling(ctx, W, H, seconds, u, strength); pools(ctx, W, H, seconds, random, strength, 2); }
    else if (style === "grain") { pools(ctx, W, H, seconds, random, strength); grain(ctx, A, { w: W, h: H, opacity: 0.06 * strength }); }
    else if (style === "gradient") pools(ctx, W, H, seconds, random, strength, 5);
    else if (style === "shapes") { pools(ctx, W, H, seconds, random, strength, 2); shapes(ctx, W, H, seconds, random, u, strength); }
    if ((instance.fields.framing || "behind") === "pip")
      speakerWindow(ctx, instance, A, W, H, u, state);
    ctx.restore();
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
