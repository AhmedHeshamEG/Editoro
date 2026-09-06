import {
  paperCard, roundRectPath, fitText, drawLines, PALETTE, alpha,
} from "/tpl/_shared/kit.js";

const ID = "progress-bar";

function layout(instance, A, viewport) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const width = viewport.width * (variant.max_width || 0.5);
  const labelSize = 30 * scale * u;
  const barHeight = 26 * scale * u;
  return {
    u, scale, width, labelSize, barHeight,
    height: labelSize * 1.6 + barHeight + 52 * scale * u,
    percent: Math.max(0, Math.min(100, Number(instance.fields.percent ?? 0))),
  };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas);
    const colour = instance.fields.colour || A.categoryColor(ID);
    const card = A.motion(instance, t, "card");
    const bar = A.motion(instance, t, "bar");
    const value = A.motion(instance, t, "value");
    const pad = 30 * g.scale * g.u;
    const barWidth = g.width - pad * 2;
    const barTop = g.height / 2 - pad - g.barHeight;

    A.stage(ctx, card, () => {
      A.shadow(ctx, card.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 14 * g.u));
      paperCard(ctx, A, { w: g.width, h: g.height, radius: 14, seed: instance.id });
    });
    A.stage(ctx, bar, () => {
      roundRectPath(ctx, -barWidth / 2, barTop, barWidth, g.barHeight, g.barHeight / 2);
      ctx.fillStyle = alpha(PALETTE.inkFaint, 0.22);
      ctx.fill();
      // The fill is a quantity, so it rides the value ramp — the card lands on
      // the spring, the bar keeps filling after it has stopped moving.
      const filled = barWidth * (g.percent / 100) * A.value(instance, t, "bar");
      if (filled > 1) {
        ctx.save();
        roundRectPath(ctx, -barWidth / 2, barTop, barWidth, g.barHeight, g.barHeight / 2);
        ctx.clip();
        ctx.fillStyle = colour;
        ctx.fillRect(-barWidth / 2, barTop, filled, g.barHeight);
        ctx.restore();
      }
    });
    A.stage(ctx, value, () => {
      // Deliberately the bar's ramp, not the readout's own: the number has to
      // agree with the fill on every frame or the two read as separate facts.
      const shown = Math.round(g.percent * A.value(instance, t, "bar"));
      const label = String(instance.fields.label || "").trim();
      const y = -g.height / 2 + pad + g.labelSize * 0.55;
      if (label) {
        const fitted = fitText(ctx, label, {
          maxWidth: barWidth * 0.68, maxLines: 1, weight: 700,
          size: g.labelSize, minSize: 11 * g.u,
        });
        drawLines(ctx, fitted.lines, {
          size: fitted.size, weight: 700, color: PALETTE.ink,
          align: "left", x: -barWidth / 2, y,
        });
      }
      drawLines(ctx, [`${shown}%`], {
        size: g.labelSize * 1.06, weight: 800, color: colour,
        align: "right", x: barWidth / 2, y,
      });
    });
  },
  measure(instance, A, viewport) {
    const g = layout(instance, A, viewport);
    return { width: g.width, height: g.height };
  },
});
