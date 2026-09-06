import { paperCard, fitText, drawLines, roundRectPath, PALETTE } from "/tpl/_shared/kit.js";

const ID = "keyword";

/** Card geometry, shared by draw() and measure() so selection matches paint. */
function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const maxWidth = viewport.width * (variant.max_width || 0.6) * scale;
  const fitted = fitText(ctx, instance.fields.text || "…", {
    maxWidth: maxWidth - 60 * u * scale,
    maxLines: variant.max_lines || 2,
    weight: 800, size: (variant.font || 62) * scale * u, minSize: 14 * u * scale,
  });
  const width = Math.min(maxWidth, Math.max(230 * u * scale, fitted.width + 74 * u * scale));
  const height = (fitted.lines.length === 1 ? 116 : 178) * scale * u;
  return { u, scale, width, height, fitted };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const geometry = layout(instance, A, ctx.canvas, ctx);
    const { u, width, height, fitted } = geometry;
    const card = A.motion(instance, t, "card");
    const text = A.motion(instance, t, "text");
    const tilt = (A.seeded(instance.id)() - 0.5) * 0.045 - 0.018;

    A.stage(ctx, card, () => {
      ctx.rotate(tilt);
      A.shadow(ctx, card.shadowSpec, c => roundRectPath(c, -width / 2, -height / 2, width, height, 10 * u));
      paperCard(ctx, A, { w: width, h: height, radius: 10, seed: instance.id, rule: 19 });
    });
    A.stage(ctx, text, () => {
      ctx.rotate(tilt);
      drawLines(ctx, fitted.lines, {
        size: fitted.size, weight: 800, color: PALETTE.ink,
        lineHeight: 1.06, y: 2 * u,
      });
    });
  },
  measure(instance, A, viewport) {
    const probe = document.createElement("canvas").getContext("2d");
    probe.canvas.width = viewport.width; probe.canvas.height = viewport.height;
    const geometry = layout(instance, A, viewport, probe);
    return { width: geometry.width, height: geometry.height };
  },
});
