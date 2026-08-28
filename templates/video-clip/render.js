import {
  paperCard, roundRectPath, fitContain, placeholder, grain,
} from "/tpl/_shared/kit.js";

const ID = "video-clip";

function layout(instance, A, viewport) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const clip = A.video(A.projectAsset(instance.fields.asset));
  const ready = Boolean(clip && clip.readyState >= 2 && clip.videoWidth);
  const box = fitContain(
    ready ? clip.videoWidth : 16,
    ready ? clip.videoHeight : 9,
    viewport.width * (variant.max_width || 0.44) * scale,
    viewport.height * (variant.max_height || 0.56) * scale,
  );
  const margin = 18 * u * scale;
  return { u, scale, clip, ready, box, margin,
    width: box.w + margin * 2, height: box.h + margin * 2 };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas);
    const tilt = (A.seeded(instance.id)() - 0.5) * 0.05;
    const frame = A.motion(instance, t, "frame");
    const clip = A.motion(instance, t, "clip");

    A.stage(ctx, frame, () => {
      ctx.rotate(tilt);
      A.shadow(ctx, frame.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 8 * g.u));
      paperCard(ctx, A, {
        w: g.width, h: g.height, radius: 8, seed: instance.id,
        texture: "paper-white.png",
      });
    });
    A.stage(ctx, clip, () => {
      ctx.rotate(tilt);
      ctx.save();
      roundRectPath(ctx, -g.box.w / 2, -g.box.h / 2, g.box.w, g.box.h, 3 * g.u);
      ctx.clip();
      if (g.ready) ctx.drawImage(g.clip, -g.box.w / 2, -g.box.h / 2, g.box.w, g.box.h);
      else placeholder(ctx, A, { w: g.box.w, h: g.box.h, label: "VIDEO" });
      grain(ctx, A, { w: g.box.w, h: g.box.h, opacity: 0.035 });
      ctx.restore();
      roundRectPath(ctx, -g.box.w / 2, -g.box.h / 2, g.box.w, g.box.h, 3 * g.u);
      ctx.strokeStyle = "#00000030";
      ctx.lineWidth = Math.max(1, g.u);
      ctx.stroke();
    });
  },
  measure(instance, A, viewport) {
    const g = layout(instance, A, viewport);
    return { width: g.width, height: g.height };
  },
});
