import { roundRectPath, fitText, drawLines, alpha } from "/tpl/_shared/kit.js";

const ID = "citation";

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const maxWidth = viewport.width * (variant.max_width || 0.4);
  const kindSize = 16 * scale * u;
  const fitted = fitText(ctx, instance.fields.text || "…", {
    maxWidth: maxWidth - 44 * u * scale, maxLines: 2, weight: 600, family: "Inter",
    size: 24 * scale * u, minSize: 10 * u,
  });
  return {
    u, scale, fitted, kindSize,
    width: Math.min(maxWidth, fitted.width + 44 * u * scale),
    height: kindSize * 1.9 + fitted.lines.length * fitted.size * 1.24 + 26 * u * scale,
  };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const state = A.motion(instance, t);
    const accent = A.categoryColor(ID);
    A.stage(ctx, state, () => {
      A.shadow(ctx, state.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 7 * g.u));
      roundRectPath(ctx, -g.width / 2, -g.height / 2, g.width, g.height, 7 * g.u);
      ctx.fillStyle = "#0d0e11d9";
      ctx.fill();
      ctx.strokeStyle = alpha(accent, 0.5);
      ctx.lineWidth = Math.max(1, 1.6 * g.u);
      ctx.stroke();
      ctx.fillStyle = accent;
      ctx.fillRect(-g.width / 2, -g.height / 2, 4 * g.u, g.height);
      const left = -g.width / 2 + 20 * g.scale * g.u;
      drawLines(ctx, [String(instance.fields.kind || "SOURCE").toUpperCase()], {
        size: g.kindSize, weight: 800, family: "Inter", color: accent,
        tracking: g.kindSize * 0.16, align: "left",
        x: left, y: -g.height / 2 + g.kindSize * 1.3,
      });
      drawLines(ctx, g.fitted.lines, {
        size: g.fitted.size, weight: 600, family: "Inter", color: "#dfe1e6",
        lineHeight: 1.24, align: "left", x: left,
        y: -g.height / 2 + g.kindSize * 1.9
          + g.fitted.lines.length * g.fitted.size * 1.24 / 2 + 4 * g.u,
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
