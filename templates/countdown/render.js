import { fitText, drawLines, alpha } from "/tpl/_shared/kit.js";

const ID = "countdown";

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const accent = A.categoryColor(ID);
    const from = Math.max(1, Math.min(10, Math.round(instance.fields.from ?? 3)));
    const step = 1 / from;
    const index = Math.min(from - 1, Math.floor(t / step));
    const withinStep = (t - index * step) / step;
    const remaining = from - index;
    const state = A.motion(instance, t);

    // Each number gets its own beat: it lands hard and then relaxes, so the
    // count reads as separate ticks rather than one long fade.
    const pop = 1.28 - 0.28 * A.ease.outBack(Math.min(1, withinStep * 3.2));
    const fade = 1 - Math.max(0, (withinStep - 0.72) / 0.28) ** 2;

    ctx.save();
    ctx.globalAlpha = state.opacity * fade;
    ctx.translate(W / 2, H * 0.44);
    ctx.scale(pop, pop);
    const ring = Math.min(W, H) * 0.17 * instance.scale;
    ctx.beginPath();
    ctx.arc(0, 0, ring, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * (1 - withinStep));
    ctx.strokeStyle = alpha(accent, 0.85);
    ctx.lineWidth = Math.max(3, 9 * u);
    ctx.lineCap = "round";
    ctx.stroke();
    drawLines(ctx, [String(remaining)], {
      size: ring * 1.25, weight: 800, color: "#f7f5f0",
      y: ring * 0.02, stroke: "#00000090", strokeWidth: ring * 0.08,
    });
    ctx.restore();

    const label = String(instance.fields.label || "").trim();
    if (!label) return;
    ctx.save();
    ctx.globalAlpha = state.opacity;
    ctx.translate(W / 2, H * 0.44 + Math.min(W, H) * 0.26 * instance.scale);
    const fitted = fitText(ctx, label.toUpperCase(), {
      maxWidth: W * 0.6, maxLines: 1, weight: 800, family: "Inter",
      size: 34 * u, minSize: 12 * u,
    });
    drawLines(ctx, fitted.lines, {
      size: fitted.size, weight: 800, family: "Inter", color: accent,
      tracking: fitted.size * 0.16,
    });
    ctx.restore();
  },
  measure(instance, A, viewport) {
    const ring = Math.min(viewport.width, viewport.height) * 0.4 * instance.scale;
    return { width: ring, height: ring };
  },
});
