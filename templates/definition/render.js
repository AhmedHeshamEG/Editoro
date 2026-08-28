import {
  paperCard, roundRectPath, fitText, drawLines, PALETTE, alpha,
} from "/tpl/_shared/kit.js";

const ID = "definition";

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const maxWidth = viewport.width * (variant.max_width || 0.56);
  const inner = maxWidth - 76 * u * scale;
  const term = fitText(ctx, instance.fields.term || "…", {
    maxWidth: inner, maxLines: 1, weight: 800, size: 56 * scale * u, minSize: 18 * u,
  });
  const meaning = fitText(ctx, instance.fields.meaning || "", {
    maxWidth: inner, maxLines: variant.max_lines || 3, weight: 500,
    size: 32 * scale * u, minSize: 13 * u,
  });
  const hasMeaning = Boolean(String(instance.fields.meaning || "").trim());
  const meaningHeight = hasMeaning ? meaning.lines.length * meaning.size * 1.30 : 0;
  return {
    u, scale, term, meaning, hasMeaning, meaningHeight, inner,
    width: Math.min(maxWidth, Math.max(340 * u * scale,
      Math.max(term.width, meaning.width) + 76 * u * scale)),
    height: term.size * 1.30 + meaningHeight + 72 * u * scale,
  };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const card = A.motion(instance, t, "card");
    const term = A.motion(instance, t, "term");
    const meaning = A.motion(instance, t, "meaning");
    const left = -g.width / 2 + 38 * g.scale * g.u;
    const top = -g.height / 2 + 34 * g.scale * g.u;

    A.stage(ctx, card, () => {
      A.shadow(ctx, card.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 12 * g.u));
      paperCard(ctx, A, { w: g.width, h: g.height, radius: 12, seed: instance.id });
      ctx.fillStyle = A.categoryColor("definition");
      roundRectPath(ctx, -g.width / 2, -g.height / 2, 7 * g.u, g.height, 3 * g.u);
      ctx.fill();
    });
    A.stage(ctx, term, () => {
      drawLines(ctx, g.term.lines, {
        size: g.term.size, weight: 800, color: PALETTE.ink,
        align: "left", x: left, y: top + g.term.size * 0.5,
      });
      const kind = String(instance.fields.kind || "").trim();
      if (kind) {
        drawLines(ctx, [kind], {
          size: g.term.size * 0.38, weight: 500, color: alpha(PALETTE.inkFaint, 0.9),
          align: "left", x: left + g.term.width + 14 * g.scale * g.u,
          y: top + g.term.size * 0.62,
        });
      }
    });
    if (!g.hasMeaning) return;
    A.stage(ctx, meaning, () => {
      drawLines(ctx, g.meaning.lines, {
        size: g.meaning.size, weight: 500, lineHeight: 1.30, color: PALETTE.inkSoft,
        align: "left", x: left,
        y: top + g.term.size * 1.28 + g.meaningHeight / 2,
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
