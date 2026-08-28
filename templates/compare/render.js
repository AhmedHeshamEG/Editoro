import {
  paperCard, roundRectPath, fitText, drawLines, PALETTE,
} from "/tpl/_shared/kit.js";

const ID = "compare";

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const stack = Boolean(variant.stack);
  const total = viewport.width * (variant.max_width || 0.72);
  const gap = 46 * scale * u;
  const cardWidth = stack ? total : (total - gap) / 2;
  const titleSize = (stack ? 42 : 44) * scale * u;
  const noteSize = titleSize * 0.52;
  const cardHeight = titleSize * 1.5 + noteSize * 2.0 + 56 * scale * u;
  return {
    u, scale, stack, gap, cardWidth, cardHeight, titleSize, noteSize,
    width: stack ? cardWidth : total,
    height: stack ? cardHeight * 2 + gap : cardHeight,
    fit: (text, size, maxLines) => fitText(ctx, text || "", {
      maxWidth: cardWidth - 44 * scale * u, maxLines, weight: maxLines === 1 ? 800 : 500,
      size, minSize: 11 * u,
    }),
  };
}

function side(ctx, A, instance, g, state, { title, note, cx, cy, seed, tint }) {
  A.stage(ctx, state, () => {
    ctx.translate(cx, cy);
    A.shadow(ctx, state.shadowSpec,
      c => roundRectPath(c, -g.cardWidth / 2, -g.cardHeight / 2, g.cardWidth, g.cardHeight, 13 * g.u));
    paperCard(ctx, A, { w: g.cardWidth, h: g.cardHeight, radius: 13, seed });
    roundRectPath(ctx, -g.cardWidth / 2, -g.cardHeight / 2, g.cardWidth, 6 * g.u, 3 * g.u);
    ctx.fillStyle = tint;
    ctx.fill();
    const titleFit = g.fit(title, g.titleSize, 1);
    drawLines(ctx, titleFit.lines, {
      size: titleFit.size, weight: 800, color: PALETTE.ink,
      y: note ? -g.noteSize * 0.9 : 0,
    });
    if (!note) return;
    const noteFit = g.fit(note, g.noteSize, 2);
    drawLines(ctx, noteFit.lines, {
      size: noteFit.size, weight: 500, color: PALETTE.inkSoft, lineHeight: 1.24,
      y: g.titleSize * 0.72,
    });
  });
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const accent = A.categoryColor("compare");
    const offset = g.stack
      ? { left: [0, -(g.cardHeight + g.gap) / 2], right: [0, (g.cardHeight + g.gap) / 2] }
      : { left: [-(g.cardWidth + g.gap) / 2, 0], right: [(g.cardWidth + g.gap) / 2, 0] };

    side(ctx, A, instance, g, A.motion(instance, t, "left"), {
      title: instance.fields.left, note: instance.fields.left_note,
      cx: offset.left[0], cy: offset.left[1], seed: instance.id + "L", tint: accent,
    });
    side(ctx, A, instance, g, A.motion(instance, t, "right"), {
      title: instance.fields.right, note: instance.fields.right_note,
      cx: offset.right[0], cy: offset.right[1], seed: instance.id + "R",
      tint: PALETTE.inkSoft,
    });

    const badgeText = String(instance.fields.badge || "VS").trim();
    if (!badgeText) return;
    const badge = A.motion(instance, t, "badge");
    A.stage(ctx, badge, () => {
      const radius = 40 * g.scale * g.u;
      A.shadow(ctx, { ...badge.shadowSpec, elevation: 16 },
        c => { c.beginPath(); c.arc(0, 0, radius, 0, 7); });
      ctx.beginPath();
      ctx.arc(0, 0, radius, 0, 7);
      ctx.fillStyle = PALETTE.ink;
      ctx.fill();
      drawLines(ctx, [badgeText], {
        size: radius * 0.72, weight: 800, color: "#ffffff", y: radius * 0.02,
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
