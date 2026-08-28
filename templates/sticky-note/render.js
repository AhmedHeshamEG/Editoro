import {
  paperCard, roundRectPath, fitText, drawLines, PALETTE,
} from "/tpl/_shared/kit.js";

const ID = "sticky-note";

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const size = viewport.width * (variant.max_width || 0.22);
  const fitted = fitText(ctx, instance.fields.text || "…", {
    maxWidth: size - 44 * u * scale, maxLines: 4, weight: 600,
    size: 28 * scale * u, minSize: 10 * u,
  });
  return { u, scale, fitted, width: size, height: size * 0.92 };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const state = A.motion(instance, t);
    const tilt = (A.seeded(instance.id)() - 0.5) * 0.13;
    A.stage(ctx, state, () => {
      ctx.rotate(tilt);
      A.shadow(ctx, state.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 3 * g.u));
      paperCard(ctx, A, {
        w: g.width, h: g.height, radius: 3, seed: instance.id,
        texture: "paper-note.png", border: "#00000018",
      });
      // The curled bottom-right corner: a triangle of shade plus a lighter
      // wedge. Cheap, and it is what stops a yellow square reading as a div.
      const curl = g.width * 0.16;
      ctx.beginPath();
      ctx.moveTo(g.width / 2, g.height / 2 - curl);
      ctx.lineTo(g.width / 2, g.height / 2);
      ctx.lineTo(g.width / 2 - curl, g.height / 2);
      ctx.closePath();
      ctx.fillStyle = "#00000022";
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(g.width / 2 - curl, g.height / 2);
      ctx.quadraticCurveTo(g.width / 2 - curl * 0.3, g.height / 2 - curl * 0.3,
        g.width / 2, g.height / 2 - curl);
      ctx.lineTo(g.width / 2 - curl, g.height / 2 - curl * 0.1);
      ctx.closePath();
      ctx.fillStyle = "#fff6c6";
      ctx.fill();
      drawLines(ctx, g.fitted.lines, {
        size: g.fitted.size, weight: 600, lineHeight: 1.3,
        color: PALETTE.ink, y: -g.height * 0.03,
      });
    });
  },
  measure(instance, A, viewport) {
    const probe = document.createElement("canvas").getContext("2d");
    probe.canvas.width = viewport.width; probe.canvas.height = viewport.height;
    const g = layout(instance, A, viewport, probe);
    return { width: g.width, height: g.height };
  },
});
