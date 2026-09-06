import {
  clamp, roundRectPath, fitContain, wrapLines, drawLines, placeholder,
} from "/tpl/_shared/kit.js";

const ID = "meme-frame";

function layout(instance, A, viewport) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const image = A.load(A.projectAsset(instance.fields.asset));
  const ready = Boolean(image?.complete && image.naturalWidth);
  const box = fitContain(
    ready ? image.naturalWidth : 4, ready ? image.naturalHeight : 3,
    viewport.width * (variant.max_width || 0.46) * scale,
    viewport.height * (variant.max_height || 0.7) * scale,
  );
  return { u, scale, image, ready, box, width: box.w, height: box.h };
}

/* Fit impact text at the size that was asked for.

   The shared fitText treats its size as a ceiling and shrinks the type until
   the words fit on two lines, which makes a size slider dead over most of its
   range: any line long enough to wrap is already width-limited at 1x, so
   asking for 1.5x just shrinks it back to the same pixels. Here the size is
   the instruction and the *number of lines* is what gives: bigger type wraps
   onto more lines, using whatever height the picture has. Only when the text
   cannot fit the image at all - a single word wider than the frame - does it
   shrink, so the slider stays live from end to end and then saturates instead
   of snapping. */
function fitImpact(ctx, text, { width, height, size }) {
  const maxWidth = width * 0.94;
  let current = Math.max(12, size);
  for (let attempt = 0; attempt < 40; attempt++) {
    ctx.font = `800 ${current}px Rubik`;
    ctx.letterSpacing = "0px";
    const room = Math.max(1, Math.floor((height - current * 0.44) / (current * 1.05)));
    const lines = wrapLines(ctx, text, maxWidth, room);
    const widest = Math.max(...lines.map(line => ctx.measureText(line).width));
    const clipped = lines.some(line => line.endsWith("…"));
    if ((widest <= maxWidth && !clipped) || current <= 12) {
      return { size: current, lines, width: Math.min(widest, maxWidth) };
    }
    current = Math.max(12, current * 0.94);
  }
  ctx.font = `800 ${current}px Rubik`;
  return { size: current, lines: wrapLines(ctx, text, maxWidth, 2), width: maxWidth };
}

/* Lay a line of impact text out and decide where it can actually go.

   `px` and `py` run 0..1 across the image - 0 is flush against the left or top
   edge, 1 flush against the right or bottom - and they move the whole block of
   text, however many lines it wrapped onto at whatever size. The range is
   worked out from the block's own measured extent, so the sliders can be
   dragged to either end and the text still lands on the picture rather than
   off the side of it. There is no combination of settings that produces a
   broken frame; when the text is too big to have any room to move, both ends
   of the slider centre it. */
function impact(ctx, A, text, { width, height, px, py, size }) {
  const fitted = fitImpact(ctx, String(text).toUpperCase(), { width, height, size });
  const step = fitted.size * 1.05;
  const block = step * Math.max(1, fitted.lines.length);
  const inset = fitted.size * 0.22;          // room for the stroke and a margin
  // drawLines centres the block on (x, y), so the travel each way is whatever
  // is left of the image once the block and its inset are taken out of it.
  const spanX = Math.max(0, width / 2 - inset - fitted.width / 2);
  const spanY = Math.max(0, height / 2 - inset - block / 2);
  drawLines(ctx, fitted.lines, {
    size: fitted.size, weight: 800, color: "#ffffff", lineHeight: 1.05,
    x: spanX * (clamp(px, 0, 1) * 2 - 1),
    y: spanY * (clamp(py, 0, 1) * 2 - 1),
    stroke: "#000000", strokeWidth: fitted.size * 0.17,
  });
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas);
    const image = A.motion(instance, t, "image");
    const top = A.motion(instance, t, "top");
    const bottom = A.motion(instance, t, "bottom");
    // Scaled off the image, not the frame, so a given size setting reads the
    // same whether the meme is filling the shot or tucked into a corner.
    const zoom = clamp(instance.fields.text_size ?? 1, 0.5, 2);
    const size = Math.max(18 * g.u, g.height * 0.15) * zoom;

    A.stage(ctx, image, () => {
      A.shadow(ctx, image.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 6 * g.u));
      ctx.save();
      roundRectPath(ctx, -g.width / 2, -g.height / 2, g.width, g.height, 6 * g.u);
      ctx.clip();
      if (g.ready) ctx.drawImage(g.image, -g.width / 2, -g.height / 2, g.width, g.height);
      else placeholder(ctx, A, { w: g.width, h: g.height, label: "MEME" });
      ctx.restore();
    });
    const topText = String(instance.fields.top || "").trim();
    if (topText) {
      A.stage(ctx, top, () => impact(ctx, A, topText, {
        width: g.width, height: g.height, size,
        px: instance.fields.top_x ?? 0.5, py: instance.fields.top_y ?? 0,
      }));
    }
    const bottomText = String(instance.fields.bottom || "").trim();
    if (bottomText) {
      A.stage(ctx, bottom, () => impact(ctx, A, bottomText, {
        width: g.width, height: g.height, size,
        px: instance.fields.bottom_x ?? 0.5, py: instance.fields.bottom_y ?? 1,
      }));
    }
  },
  measure(instance, A, viewport) {
    const g = layout(instance, A, viewport);
    return { width: g.width, height: g.height };
  },
});
