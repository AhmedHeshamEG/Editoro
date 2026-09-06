import {
  paperCard, roundRectPath, fitText, drawLines, PALETTE, alpha,
} from "/tpl/_shared/kit.js";

const ID = "equation";

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const maxWidth = viewport.width * (variant.max_width || 0.52) * scale;
  const fitted = fitText(ctx, instance.fields.text || "…", {
    maxWidth: maxWidth - 92 * u * scale, maxLines: 2, weight: 600,
    size: 72 * scale * u, minSize: 18 * u * scale,
  });
  const caption = String(instance.fields.caption || "").trim();
  const captionSize = Math.max(12 * u, fitted.size * 0.32);
  return {
    u, scale, fitted, caption, captionSize,
    width: Math.min(maxWidth, Math.max(320 * u * scale, fitted.width + 96 * u * scale)),
    height: fitted.lines.length * fitted.size * 1.24
      + (caption ? captionSize * 2.4 : 0) + 62 * u * scale,
  };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const card = A.motion(instance, t, "card");
    const formula = A.motion(instance, t, "formula");
    const caption = A.motion(instance, t, "caption");

    A.stage(ctx, card, () => {
      A.shadow(ctx, card.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 11 * g.u));
      paperCard(ctx, A, { w: g.width, h: g.height, radius: 11, seed: instance.id });
      // Squared paper: the grid is what says "worked out by hand" rather than
      // "typeset", which is the whole reason this card exists.
      ctx.save();
      roundRectPath(ctx, -g.width / 2, -g.height / 2, g.width, g.height, 11 * g.u);
      ctx.clip();
      ctx.strokeStyle = alpha(PALETTE.rule, 0.20);
      ctx.lineWidth = Math.max(0.6, g.u * 0.8);
      const cell = 22 * g.scale * g.u;
      for (let x = -g.width / 2; x < g.width / 2; x += cell) {
        ctx.beginPath(); ctx.moveTo(x, -g.height / 2); ctx.lineTo(x, g.height / 2); ctx.stroke();
      }
      for (let y = -g.height / 2; y < g.height / 2; y += cell) {
        ctx.beginPath(); ctx.moveTo(-g.width / 2, y); ctx.lineTo(g.width / 2, y); ctx.stroke();
      }
      ctx.restore();
    });
    A.stage(ctx, formula, () => {
      drawLines(ctx, g.fitted.lines, {
        size: g.fitted.size, weight: 600, lineHeight: 1.24, color: PALETTE.ink,
        y: g.caption ? -g.captionSize * 1.1 : 0,
      });
    });
    if (!g.caption) return;
    A.stage(ctx, caption, () => {
      drawLines(ctx, [g.caption], {
        size: g.captionSize, weight: 600, color: PALETTE.inkFaint,
        y: g.height / 2 - g.captionSize * 1.5,
      });
    });
  },
  measure(instance, A, viewport) {
    const probe = document.createElement("canvas").getContext("2d");
    probe.canvas.width = viewport.width; probe.canvas.height = viewport.height;
    const g = layout(instance, A, viewport, probe);
    return { width: g.width, height: g.height };
  },
});
