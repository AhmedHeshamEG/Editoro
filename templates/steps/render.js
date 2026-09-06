import {
  fitText, drawLines, splitItems, PALETTE, alpha,
} from "/tpl/_shared/kit.js";

const ID = "steps";
const MAX_STEPS = 5;

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const stack = Boolean(variant.stack);
  const items = splitItems(instance.fields.items, MAX_STEPS);
  const count = Math.max(1, items.length);
  const total = viewport.width * (variant.max_width || 0.8);
  const dot = 46 * scale * u;
  const labelSize = 27 * scale * u;
  const slot = stack ? dot * 2.2 : total / count;
  const fitted = items.map(item => fitText(ctx, item, {
    maxWidth: stack ? total - dot * 2.6 : slot - 18 * scale * u,
    maxLines: 2, weight: 600, size: labelSize, minSize: 10 * u,
  }));
  return {
    u, scale, stack, items, count, dot, labelSize, slot, fitted,
    width: stack ? total : total,
    height: stack ? slot * count : dot + labelSize * 3.0,
  };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const accent = A.categoryColor("steps");
    const line = A.motion(instance, t, "line");
    const positions = g.items.map((item, index) => (g.stack
      ? { x: -g.width / 2 + g.dot, y: -g.height / 2 + g.slot * (index + 0.5) }
      : { x: -g.width / 2 + g.slot * (index + 0.5), y: -g.height / 2 + g.dot / 2 }));

    if (positions.length > 1) {
      A.stage(ctx, line, () => {
        const first = positions[0], last = positions[positions.length - 1];
        const grown = A.value(instance, t, "line");
        ctx.strokeStyle = alpha(accent, 0.45);
        ctx.lineWidth = Math.max(2, 4 * g.scale * g.u);
        ctx.setLineDash([10 * g.u, 8 * g.u]);
        ctx.beginPath();
        ctx.moveTo(first.x, first.y);
        ctx.lineTo(first.x + (last.x - first.x) * grown, first.y + (last.y - first.y) * grown);
        ctx.stroke();
        ctx.setLineDash([]);
      });
    }

    g.items.forEach((item, index) => {
      const state = A.motion(instance, t, `step${index}`);
      const at = positions[index];
      A.stage(ctx, state, () => {
        ctx.translate(at.x, at.y);
        A.shadow(ctx, state.shadowSpec, c => { c.beginPath(); c.arc(0, 0, g.dot / 2, 0, 7); });
        ctx.beginPath();
        ctx.arc(0, 0, g.dot / 2, 0, 7);
        ctx.fillStyle = accent;
        ctx.fill();
        drawLines(ctx, [String(index + 1)], {
          size: g.dot * 0.55, weight: 800, color: "#12141a", y: g.dot * 0.02,
        });
        const fit = g.fitted[index];
        if (g.stack) {
          drawLines(ctx, fit.lines, {
            size: fit.size, weight: 600, color: "#f0eee9", align: "left",
            lineHeight: 1.2, x: g.dot * 0.85, y: 0,
            stroke: "#000000c0", strokeWidth: fit.size * 0.16,
          });
        } else {
          drawLines(ctx, fit.lines, {
            size: fit.size, weight: 600, color: "#f0eee9", lineHeight: 1.2,
            y: g.dot * 0.72 + fit.size * 0.6,
            stroke: "#000000c0", strokeWidth: fit.size * 0.16,
          });
        }
        void PALETTE;
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
