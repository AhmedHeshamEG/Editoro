import { roundRectPath } from "/tpl/_shared/kit.js";

registerTemplate("spotlight", {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const rect = instance.fields.rect || { x: 0.34, y: 0.28, w: 0.32, h: 0.36 };
    const state = A.motion(instance, t);
    const dim = Math.max(0.1, Math.min(0.95, instance.fields.dim ?? 0.62));
    const cx = (rect.x + rect.w / 2) * W, cy = (rect.y + rect.h / 2) * H;
    const rx = rect.w * W / 2, ry = rect.h * H / 2;
    ctx.save();
    ctx.globalAlpha = state.opacity;
    // The dark field is drawn first and then punched through with the lit
    // shape, so the edge is a real soft falloff instead of a pasted vignette.
    ctx.fillStyle = `rgba(6,7,9,${dim})`;
    ctx.fillRect(0, 0, W, H);
    ctx.globalCompositeOperation = "destination-out";
    if ((instance.fields.shape || "ellipse") === "ellipse") {
      const gradient = ctx.createRadialGradient(cx, cy, Math.min(rx, ry) * 0.55, cx, cy,
        Math.max(rx, ry) * 1.12);
      gradient.addColorStop(0, "#ffffff");
      gradient.addColorStop(1, "#ffffff00");
      ctx.fillStyle = gradient;
      ctx.save();
      ctx.translate(cx, cy);
      ctx.scale(1, ry / Math.max(1, rx));
      ctx.translate(-cx, -cy);
      ctx.fillRect(cx - rx * 2, cy - rx * 2, rx * 4, rx * 4);
      ctx.restore();
    } else {
      ctx.filter = `blur(${Math.max(4, Math.min(rx, ry) * 0.12)}px)`;
      roundRectPath(ctx, cx - rx, cy - ry, rx * 2, ry * 2, Math.min(rx, ry) * 0.22);
      ctx.fillStyle = "#ffffff";
      ctx.fill();
      ctx.filter = "none";
    }
    ctx.restore();
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
