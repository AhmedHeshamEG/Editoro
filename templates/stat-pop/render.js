import {
  paperCard, roundRectPath, fitText, drawLines, formatNumber, PALETTE,
} from "/tpl/_shared/kit.js";

const ID = "stat-pop";

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const maxWidth = viewport.width * (variant.max_width || 0.42);
  const label = String(instance.fields.label || "").trim();
  const numberSize = 132 * scale * u;
  const target = Number(String(instance.fields.value ?? "").replace(/[^0-9.\-]/g, "")) || 0;
  const decimals = Math.max(0, Math.min(4, Math.round(instance.fields.decimals ?? 0)));
  const sample = formatNumber(target, {
    decimals, prefix: instance.fields.prefix || "", suffix: instance.fields.suffix || "",
  });
  const fitted = fitText(ctx, sample, {
    maxWidth: maxWidth - 80 * u * scale, maxLines: 1, weight: 800,
    size: numberSize, minSize: 20 * u,
  });
  const labelSize = Math.max(14 * u, fitted.size * 0.24);
  const width = Math.min(maxWidth, Math.max(300 * u * scale, fitted.width + 96 * u * scale));
  const height = (label ? fitted.size * 1.28 + labelSize * 2.2 : fitted.size * 1.5)
    + 44 * u * scale;
  return { u, scale, width, height, fitted, label, labelSize, target, decimals };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const card = A.motion(instance, t, "card");
    const number = A.motion(instance, t, "number");
    const label = A.motion(instance, t, "label");

    A.stage(ctx, card, () => {
      A.shadow(ctx, card.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 16 * g.u));
      paperCard(ctx, A, { w: g.width, h: g.height, radius: 16, seed: instance.id });
    });
    A.stage(ctx, number, () => {
      // The count-up runs on its own monotonic ramp, not on the entrance spring:
      // the card is thrown into place fast and the number keeps climbing under
      // it for a beat afterwards. That is the shape the eye expects, and it is
      // the only way the digits are on screen long enough to be read at all.
      const counted = g.target * A.value(instance, t, "number");
      const text = formatNumber(counted, {
        decimals: g.decimals,
        prefix: instance.fields.prefix || "", suffix: instance.fields.suffix || "",
      });
      drawLines(ctx, [text], {
        size: g.fitted.size, weight: 800, color: PALETTE.ink,
        y: g.label ? -g.labelSize * 0.9 : 0,
      });
    });
    if (!g.label) return;
    A.stage(ctx, label, () => {
      drawLines(ctx, [g.label.toUpperCase()], {
        size: g.labelSize, weight: 700, color: PALETTE.inkFaint,
        tracking: g.labelSize * 0.10, y: g.fitted.size * 0.52,
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
