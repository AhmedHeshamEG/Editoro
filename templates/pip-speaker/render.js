import { fitCover, placeholder, roundRectPath } from "/tpl/_shared/kit.js";

const ID = "pip-speaker";

function windowRect(instance, W, H, u) {
  const size = Math.max(0.12, Math.min(0.45, instance.fields.size ?? 0.26));
  const w = W * size, h = w * (H / W) * 1.02;
  const margin = 34 * u;
  const corner = instance.fields.corner || "bottom-right";
  const x = corner.endsWith("left") ? margin : W - margin - w;
  const y = corner.startsWith("top") ? margin : H - margin - h;
  return { x, y, w, h };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const state = A.motion(instance, t);
    const hole = windowRect(instance, W, H, u);
    const clipUrl = instance.fields.clip ? A.projectAsset(instance.fields.clip) : "";
    const media = clipUrl ? A.video(clipUrl)
      : (instance.fields.asset ? A.load(A.projectAsset(instance.fields.asset)) : null);
    const width = media ? (media.naturalWidth || media.videoWidth || 0) : 0;
    const height = media ? (media.naturalHeight || media.videoHeight || 0) : 0;

    ctx.save();
    ctx.globalAlpha = state.opacity;
    ctx.fillStyle = "#07080b";
    ctx.fillRect(0, 0, W, H);
    if (width && height) {
      const fitted = fitCover(width, height, W, H);
      ctx.drawImage(media, (W - fitted.w) / 2, (H - fitted.h) / 2, fitted.w, fitted.h);
    } else {
      ctx.save();
      ctx.translate(W / 2, H / 2);
      placeholder(ctx, A, { w: W * 0.5, h: H * 0.4, label: "FULL FRAME" });
      ctx.restore();
    }
    // Cut the window out of the layer we just painted; the footage underneath
    // shows through it untouched, which is why this needs no camera work.
    ctx.globalCompositeOperation = "destination-out";
    roundRectPath(ctx, hole.x, hole.y, hole.w, hole.h, 18 * u);
    ctx.fillStyle = "#ffffff";
    ctx.fill();
    ctx.globalCompositeOperation = "source-over";
    roundRectPath(ctx, hole.x, hole.y, hole.w, hole.h, 18 * u);
    ctx.strokeStyle = "#ffffff3a";
    ctx.lineWidth = Math.max(1, 3 * u);
    ctx.stroke();
    ctx.restore();
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
