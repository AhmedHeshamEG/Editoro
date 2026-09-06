import { scribbleEllipse } from "/tpl/_shared/kit.js";

const ID = "circle-emphasis";

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const rect = instance.fields.rect || { x: 0.36, y: 0.30, w: 0.28, h: 0.30 };
    const state = A.motion(instance, t);
    A.stage(ctx, state, () => {
      ctx.translate((rect.x + rect.w / 2) * W, (rect.y + rect.h / 2) * H);
      scribbleEllipse(ctx, {
        w: rect.w * W * 1.18, h: rect.h * H * 1.22,
        progress: A.value(instance, t),
        seed: instance.id, color: instance.fields.colour || A.categoryColor(ID),
        width: Math.max(3, 9 * u * instance.scale),
        laps: Math.max(1, Math.min(3, instance.fields.laps ?? 1.5)),
      });
    });
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
