import {
  paperCard, roundRectPath, fitText, drawLines, PALETTE, alpha,
} from "/tpl/_shared/kit.js";

const ID = "quote-card";

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const maxWidth = viewport.width * (variant.max_width || 0.58) * scale;
  const inner = maxWidth - 110 * u * scale;
  const fitted = fitText(ctx, instance.fields.text || "…", {
    maxWidth: inner, maxLines: variant.max_lines || 4, weight: 600,
    size: 50 * scale * u, minSize: 16 * u * scale,
  });
  const author = String(instance.fields.author || "").trim();
  const authorSize = Math.max(13 * u, fitted.size * 0.42);
  const body = fitted.lines.length * fitted.size * 1.26;
  return {
    u, scale, fitted, author, authorSize, inner,
    width: Math.min(maxWidth, Math.max(360 * u * scale, fitted.width + 110 * u * scale)),
    height: body + (author ? authorSize * 2.6 : 0) + 96 * u * scale,
  };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const card = A.motion(instance, t, "card");
    const mark = A.motion(instance, t, "mark");
    const text = A.motion(instance, t, "text");
    const author = A.motion(instance, t, "author");
    const bodyHeight = g.fitted.lines.length * g.fitted.size * 1.26;

    A.stage(ctx, card, () => {
      A.shadow(ctx, card.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 14 * g.u));
      paperCard(ctx, A, { w: g.width, h: g.height, radius: 14, seed: instance.id, rule: 24 });
    });
    A.stage(ctx, mark, () => {
      const markSize = 118 * g.scale * g.u;
      drawLines(ctx, ["\u201C"], {
        size: markSize, weight: 800,
        color: alpha(PALETTE.inkFaint, 0.30), align: "center",
        x: -g.width / 2 + markSize * 0.42,
        y: -g.height / 2 + markSize * 0.46,
      });
    });
    A.stage(ctx, text, () => {
      drawLines(ctx, g.fitted.lines, {
        size: g.fitted.size, weight: 600, lineHeight: 1.26, color: PALETTE.ink,
        y: g.author ? -g.authorSize * 1.2 : 0,
      });
    });
    if (!g.author) return;
    A.stage(ctx, author, () => {
      const y = bodyHeight / 2 + g.authorSize * 0.6;
      ctx.strokeStyle = alpha(PALETTE.inkFaint, 0.4);
      ctx.lineWidth = Math.max(1, g.u);
      ctx.beginPath();
      ctx.moveTo(-g.authorSize * 1.6, y - g.authorSize * 0.85);
      ctx.lineTo(g.authorSize * 1.6, y - g.authorSize * 0.85);
      ctx.stroke();
      drawLines(ctx, [g.author], {
        size: g.authorSize, weight: 700, color: PALETTE.inkSoft, y,
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
