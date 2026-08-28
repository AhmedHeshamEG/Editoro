import {
  paperCard, roundRectPath, fitContain, highlighterSweep, placeholder,
} from "/tpl/_shared/kit.js";

const ID = "highlight";
const SWEEP_START = 0.14;
const SWEEP_SPAN = 0.58;

function pageBox(instance, A, viewport) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const image = A.load(A.instanceAssetUrl(instance));
  const ready = Boolean(image?.complete && image.naturalWidth);
  const box = fitContain(
    ready ? image.naturalWidth : 3,
    ready ? image.naturalHeight : 4,
    viewport.width * (variant.max_width || 0.46) * scale,
    viewport.height * (variant.max_height || 0.8) * scale,
  );
  return { u, scale, image, ready, box, margin: 16 * u * scale };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = pageBox(instance, A, ctx.canvas);
    const width = g.box.w + g.margin * 2, height = g.box.h + g.margin * 2;
    const page = A.motion(instance, t, "page");
    const marks = A.motion(instance, t, "marks");

    A.stage(ctx, page, () => {
      A.shadow(ctx, page.shadowSpec,
        c => roundRectPath(c, -width / 2, -height / 2, width, height, 5 * g.u));
      paperCard(ctx, A, { w: width, h: height, radius: 5, seed: instance.id,
        texture: "paper-white.png" });
      ctx.save();
      roundRectPath(ctx, -g.box.w / 2, -g.box.h / 2, g.box.w, g.box.h, 2 * g.u);
      ctx.clip();
      if (g.ready) ctx.drawImage(g.image, -g.box.w / 2, -g.box.h / 2, g.box.w, g.box.h);
      else placeholder(ctx, A, { w: g.box.w, h: g.box.h, label: "PAGE" });
      ctx.restore();
    });

    const strokes = Array.isArray(instance.fields.strokes) ? instance.fields.strokes : [];
    if (!strokes.length) return;
    // Each stroke starts after the one before it finishes: the marker is one
    // hand moving down the page, not several strokes appearing at once.
    const overall = A.ease.outCubic(
      Math.max(0, Math.min(1, (t - SWEEP_START) / SWEEP_SPAN)));
    A.stage(ctx, marks, () => {
      strokes.forEach((stroke, index) => {
        const progress = Math.max(0, Math.min(1, overall * strokes.length - index));
        if (progress <= 0) return;
        const x1 = -g.box.w / 2 + Math.min(stroke.x1, stroke.x2) * g.box.w;
        const x2 = -g.box.w / 2 + Math.max(stroke.x1, stroke.x2) * g.box.w;
        const y = -g.box.h / 2 + stroke.y * g.box.h;
        highlighterSweep(ctx, A, {
          x: x1, y, w: x2 - x1,
          h: (stroke.h ? stroke.h * g.box.h : g.box.h * 0.038),
          progress: A.ease.outQuad(progress), opacity: 0.82,
        });
      });
    });
  },
  /** Bounds of the page itself — the stroke editor draws in these coordinates. */
  contentMeasure(instance, A, viewport) {
    const g = pageBox(instance, A, viewport);
    return { width: g.box.w, height: g.box.h };
  },
  measure(instance, A, viewport) {
    const g = pageBox(instance, A, viewport);
    return { width: g.box.w + g.margin * 2, height: g.box.h + g.margin * 2 };
  },
});
