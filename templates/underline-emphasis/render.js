import { scribbleUnderline, highlighterSweep } from "/tpl/_shared/kit.js";

const ID = "underline-emphasis";

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const rect = instance.fields.rect || { x: 0.30, y: 0.60, w: 0.40, h: 0.10 };
    const state = A.motion(instance, t);
    const progress = state.phase === "in" ? state.k : 1;
    const width = rect.w * W;
    const centreX = (rect.x + rect.w / 2) * W;
    const baseline = (rect.y + rect.h) * H;
    A.stage(ctx, state, () => {
      if ((instance.fields.style || "pen") === "marker") {
        highlighterSweep(ctx, A, {
          x: centreX - width / 2, y: baseline - rect.h * H * 0.42,
          w: width, h: rect.h * H * 0.72, progress,
          color: instance.fields.colour || A.categoryColor(ID), opacity: 0.72,
        });
        return;
      }
      ctx.translate(centreX, baseline);
      scribbleUnderline(ctx, {
        w: width, progress, seed: instance.id,
        color: instance.fields.colour || A.categoryColor(ID),
        width: Math.max(3, 8 * u * instance.scale),
        sag: 6 * u * instance.scale,
      });
    });
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
