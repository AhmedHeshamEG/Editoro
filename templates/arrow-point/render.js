import { handArrow, fitText, drawLines } from "/tpl/_shared/kit.js";

const ID = "arrow-point";

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const target = instance.fields.rect || { x: 0.55, y: 0.35, w: 0.18, h: 0.18 };
    const tail = instance.fields.rect2 || { x: 0.20, y: 0.62, w: 0.10, h: 0.10 };
    const colour = instance.fields.colour || A.categoryColor(ID);
    const from = { x: (tail.x + tail.w / 2) * W, y: (tail.y + tail.h / 2) * H };
    const to = { x: (target.x + target.w / 2) * W, y: (target.y + target.h / 2) * H };
    // Stop short of the target so the head points at the thing rather than
    // covering it.
    const angle = Math.atan2(to.y - from.y, to.x - from.x);
    const inset = Math.min(target.w * W, target.h * H) * 0.55;
    const tip = { x: to.x - Math.cos(angle) * inset, y: to.y - Math.sin(angle) * inset };

    const arrow = A.motion(instance, t, "arrow");
    A.stage(ctx, arrow, () => {
      handArrow(ctx, {
        x1: from.x, y1: from.y, x2: tip.x, y2: tip.y,
        bend: 0.20, width: Math.max(3, 8 * u * instance.scale),
        color: colour, head: 34 * u * instance.scale,
        progress: A.value(instance, t, "arrow"),
      });
    });
    const label = String(instance.fields.label || "").trim();
    if (!label) return;
    const state = A.motion(instance, t, "label");
    A.stage(ctx, state, () => {
      const fitted = fitText(ctx, label, {
        maxWidth: W * 0.3, maxLines: 1, weight: 800,
        size: 40 * u * instance.scale, minSize: 12 * u * scale,
      });
      drawLines(ctx, fitted.lines, {
        size: fitted.size, weight: 800, color: colour,
        x: from.x, y: from.y - fitted.size * 1.05,
        stroke: "#000000b0", strokeWidth: fitted.size * 0.16,
      });
    });
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
