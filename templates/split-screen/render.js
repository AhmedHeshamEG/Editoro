import { fitCover, placeholder } from "/tpl/_shared/kit.js";

const ID = "split-screen";

function panelRect(side, W, H) {
  if (side === "left") return { x: 0, y: 0, w: W / 2, h: H };
  if (side === "top") return { x: 0, y: 0, w: W, h: H / 2 };
  if (side === "bottom") return { x: 0, y: H / 2, w: W, h: H / 2 };
  return { x: W / 2, y: 0, w: W / 2, h: H };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const side = instance.fields.side || "right";
    const panel = panelRect(side, W, H);
    const state = A.motion(instance, t);
    const clipUrl = instance.fields.clip ? A.projectAsset(instance.fields.clip) : "";
    const media = clipUrl ? A.video(clipUrl)
      : (instance.fields.asset ? A.load(A.projectAsset(instance.fields.asset)) : null);
    const width = media ? (media.naturalWidth || media.videoWidth || 0) : 0;
    const height = media ? (media.naturalHeight || media.videoHeight || 0) : 0;

    A.stage(ctx, state, () => {
      ctx.save();
      ctx.beginPath();
      ctx.rect(panel.x, panel.y, panel.w, panel.h);
      ctx.clip();
      ctx.fillStyle = "#0a0b0e";
      ctx.fillRect(panel.x, panel.y, panel.w, panel.h);
      if (width && height) {
        const fitted = fitCover(width, height, panel.w, panel.h);
        ctx.drawImage(media,
          panel.x + (panel.w - fitted.w) / 2, panel.y + (panel.h - fitted.h) / 2,
          fitted.w, fitted.h);
      } else {
        ctx.translate(panel.x + panel.w / 2, panel.y + panel.h / 2);
        placeholder(ctx, A, { w: panel.w * 0.6, h: panel.h * 0.4, label: "PANEL" });
      }
      ctx.restore();
      // A hairline seam, lit from the speaker's side, so the join reads as a
      // deliberate split rather than a rectangle dropped on the footage.
      ctx.fillStyle = "#ffffff26";
      if (side === "left" || side === "right") {
        ctx.fillRect(side === "right" ? panel.x : panel.x + panel.w - 2 * u, 0, 2 * u, H);
      } else {
        ctx.fillRect(0, side === "bottom" ? panel.y : panel.y + panel.h - 2 * u, W, 2 * u);
      }
    });
  },
  measure(instance, A, viewport) {
    const panel = panelRect(instance.fields.side || "right", viewport.width, viewport.height);
    return { width: panel.w, height: panel.h };
  },
});
