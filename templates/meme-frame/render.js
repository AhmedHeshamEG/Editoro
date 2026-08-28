import {
  roundRectPath, fitContain, fitText, drawLines, placeholder,
} from "/tpl/_shared/kit.js";

const ID = "meme-frame";

function layout(instance, A, viewport) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const image = A.load(A.projectAsset(instance.fields.asset));
  const ready = Boolean(image?.complete && image.naturalWidth);
  const box = fitContain(
    ready ? image.naturalWidth : 4, ready ? image.naturalHeight : 3,
    viewport.width * (variant.max_width || 0.46) * scale,
    viewport.height * (variant.max_height || 0.7) * scale,
  );
  return { u, scale, image, ready, box, width: box.w, height: box.h };
}

function impact(ctx, A, text, { width, y, size }) {
  const fitted = fitText(ctx, String(text).toUpperCase(), {
    maxWidth: width * 0.94, maxLines: 2, weight: 800, size, minSize: 12,
  });
  drawLines(ctx, fitted.lines, {
    size: fitted.size, weight: 800, color: "#ffffff", lineHeight: 1.05, y,
    stroke: "#000000", strokeWidth: fitted.size * 0.17,
  });
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas);
    const image = A.motion(instance, t, "image");
    const top = A.motion(instance, t, "top");
    const bottom = A.motion(instance, t, "bottom");
    const size = Math.max(18 * g.u, g.height * 0.15);

    A.stage(ctx, image, () => {
      A.shadow(ctx, image.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 6 * g.u));
      ctx.save();
      roundRectPath(ctx, -g.width / 2, -g.height / 2, g.width, g.height, 6 * g.u);
      ctx.clip();
      if (g.ready) ctx.drawImage(g.image, -g.width / 2, -g.height / 2, g.width, g.height);
      else placeholder(ctx, A, { w: g.width, h: g.height, label: "MEME" });
      ctx.restore();
    });
    const topText = String(instance.fields.top || "").trim();
    if (topText) {
      A.stage(ctx, top, () => impact(ctx, A, topText, {
        width: g.width, y: -g.height / 2 + size * 0.95, size,
      }));
    }
    const bottomText = String(instance.fields.bottom || "").trim();
    if (bottomText) {
      A.stage(ctx, bottom, () => impact(ctx, A, bottomText, {
        width: g.width, y: g.height / 2 - size * 0.95, size,
      }));
    }
  },
  measure(instance, A, viewport) {
    const g = layout(instance, A, viewport);
    return { width: g.width, height: g.height };
  },
});
