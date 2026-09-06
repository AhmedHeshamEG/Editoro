import { darkPanel, roundRectPath, drawLines, alpha } from "/tpl/_shared/kit.js";

const ID = "code-card";
const MAX_LINES = 12;
const MONO = 'ui-monospace, "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace';

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const width = viewport.width * (variant.max_width || 0.48) * scale;
  const lines = String(instance.fields.code || "")
    .replace(/\t/g, "  ").split("\n").slice(0, MAX_LINES);
  if (!lines.length) lines.push("");
  const pad = 26 * scale * u;
  const gutter = 46 * scale * u;
  let size = 27 * scale * u;
  for (let attempt = 0; attempt < 20; attempt++) {
    ctx.font = `500 ${size}px ${MONO}`;
    const widest = Math.max(...lines.map(line => ctx.measureText(line).width));
    if (widest <= width - pad * 2 - gutter || size <= 9 * u) break;
    size *= 0.94;
  }
  const title = String(instance.fields.title || "").trim();
  const bar = title ? 40 * scale * u : 0;
  const step = size * 1.52;
  return { u, scale, width, lines, pad, gutter, size, step, title, bar,
    height: bar + pad * 2 + lines.length * step };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const panel = A.motion(instance, t, "panel");
    const code = A.motion(instance, t, "code");
    const focusState = A.motion(instance, t, "focus");
    const focus = Math.round(instance.fields.focus ?? 0);
    const top = -g.height / 2;
    const textLeft = -g.width / 2 + g.pad + g.gutter;

    A.stage(ctx, panel, () => {
      A.shadow(ctx, panel.shadowSpec,
        c => roundRectPath(c, -g.width / 2, top, g.width, g.height, 14 * g.u));
      darkPanel(ctx, A, { w: g.width, h: g.height, radius: 14, fill: "#171a21" });
      if (!g.title) return;
      ctx.strokeStyle = "#ffffff12";
      ctx.lineWidth = Math.max(1, g.u);
      ctx.beginPath();
      ctx.moveTo(-g.width / 2 + 4 * g.u, top + g.bar);
      ctx.lineTo(g.width / 2 - 4 * g.u, top + g.bar);
      ctx.stroke();
      drawLines(ctx, [g.title], {
        size: g.bar * 0.42, weight: 600, family: "Inter", color: "#9aa1ad",
        align: "left", x: -g.width / 2 + g.pad, y: top + g.bar / 2,
      });
    });
    if (focus >= 1 && focus <= g.lines.length) {
      A.stage(ctx, focusState, () => {
        const y = top + g.bar + g.pad + (focus - 1) * g.step;
        roundRectPath(ctx, -g.width / 2 + 8 * g.u, y - g.step * 0.08,
          g.width - 16 * g.u, g.step, 5 * g.u);
        ctx.fillStyle = alpha(A.categoryColor(ID), 0.16);
        ctx.fill();
        ctx.fillStyle = A.categoryColor(ID);
        ctx.fillRect(-g.width / 2 + 8 * g.u, y - g.step * 0.08, 3.5 * g.u, g.step);
      });
    }
    A.stage(ctx, code, () => {
      g.lines.forEach((line, index) => {
        const y = top + g.bar + g.pad + index * g.step + g.step * 0.42;
        drawLines(ctx, [String(index + 1)], {
          size: g.size * 0.86, weight: 500, family: MONO,
          color: "#565d6b", align: "right",
          x: -g.width / 2 + g.pad + g.gutter - 14 * g.scale * g.u, y,
        });
        drawLines(ctx, [line], {
          size: g.size, weight: 500, family: MONO,
          color: index + 1 === focus ? "#ffffff" : "#cdd3de",
          align: "left", x: textLeft, y,
        });
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
