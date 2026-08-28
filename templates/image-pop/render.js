import {
  paperCard, roundRectPath, fitContain, tapeStrip, placeholder, drawLines,
  fitText, PALETTE, grain,
} from "/tpl/_shared/kit.js";

const ID = "image-pop";

function layout(instance, A, viewport) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const image = A.load(A.instanceAssetUrl(instance));
  const ready = Boolean(image?.complete && image.naturalWidth);
  const box = fitContain(
    ready ? image.naturalWidth : 4,
    ready ? image.naturalHeight : 3,
    viewport.width * (variant.max_width || 0.42) * scale,
    viewport.height * (variant.max_height || 0.56) * scale,
  );
  const margin = 22 * u * scale;
  const caption = String(instance.fields.caption || "").trim();
  const foot = caption ? 62 * u * scale : 34 * u * scale;
  return {
    u, scale, image, ready, photo: box, margin, caption, foot,
    width: box.w + margin * 2,
    height: box.h + margin + foot,
  };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas);
    const tilt = (A.seeded(instance.id)() - 0.5) * 0.075;
    const frame = A.motion(instance, t, "frame");
    const photo = A.motion(instance, t, "photo");
    const tape = A.motion(instance, t, "tape");
    const caption = A.motion(instance, t, "caption");
    const photoTop = -g.height / 2 + g.margin;

    A.stage(ctx, frame, () => {
      ctx.rotate(tilt);
      A.shadow(ctx, frame.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 6 * g.u));
      paperCard(ctx, A, {
        w: g.width, h: g.height, radius: 6, seed: instance.id,
        texture: "paper-white.png", lift: true,
      });
    });
    A.stage(ctx, photo, () => {
      ctx.rotate(tilt);
      ctx.save();
      roundRectPath(ctx, -g.photo.w / 2, photoTop, g.photo.w, g.photo.h, 2 * g.u);
      ctx.clip();
      if (g.ready) ctx.drawImage(g.image, -g.photo.w / 2, photoTop, g.photo.w, g.photo.h);
      else {
        ctx.translate(0, photoTop + g.photo.h / 2);
        placeholder(ctx, A, { w: g.photo.w, h: g.photo.h, label: "IMAGE" });
        ctx.translate(0, -(photoTop + g.photo.h / 2));
      }
      ctx.restore();
      ctx.save();
      ctx.translate(0, photoTop + g.photo.h / 2);
      grain(ctx, A, { w: g.photo.w, h: g.photo.h, opacity: 0.045 });
      ctx.restore();
      roundRectPath(ctx, -g.photo.w / 2, photoTop, g.photo.w, g.photo.h, 2 * g.u);
      ctx.strokeStyle = "#00000026";
      ctx.lineWidth = Math.max(1, g.u);
      ctx.stroke();
    });
    if (g.caption) {
      A.stage(ctx, caption, () => {
        ctx.rotate(tilt);
        const fitted = fitText(ctx, g.caption, {
          maxWidth: g.photo.w, maxLines: 1, weight: 600,
          size: 30 * g.scale * g.u, minSize: 11 * g.u,
        });
        drawLines(ctx, fitted.lines, {
          size: fitted.size, weight: 600, color: PALETTE.inkSoft,
          y: g.height / 2 - g.foot / 2 - 4 * g.u,
        });
      });
    }
    A.stage(ctx, tape, () => {
      ctx.rotate(tilt);
      tapeStrip(ctx, A, {
        x: -g.width * 0.28, y: -g.height / 2, w: 104 * g.scale,
        angle: -0.34, opacity: 0.9,
      });
    });
  },
  measure(instance, A, viewport) {
    const g = layout(instance, A, viewport);
    return { width: g.width, height: g.height };
  },
});
