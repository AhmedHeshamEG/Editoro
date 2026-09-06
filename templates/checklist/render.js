import {
  paperCard, roundRectPath, fitText, drawLines, splitItems, checkMark, PALETTE,
} from "/tpl/_shared/kit.js";

const ID = "checklist";
const MAX_ITEMS = 6;

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const maxWidth = viewport.width * (variant.max_width || 0.42) * scale;
  const items = splitItems(instance.fields.items, MAX_ITEMS);
  const title = String(instance.fields.title || "").trim();
  const itemSize = 34 * scale * u;
  const tick = itemSize * 1.05;
  const inner = maxWidth - (tick + 78 * u * scale);
  const fitted = items.map(item => fitText(ctx, item, {
    maxWidth: inner, maxLines: 1, weight: 600, size: itemSize, minSize: 12 * u * scale,
  }));
  const titleFit = title
    ? fitText(ctx, title, {
        maxWidth: maxWidth - 68 * u * scale, maxLines: 1, weight: 800,
        size: itemSize * 1.24, minSize: 12 * u * scale,
      })
    : null;
  const titleSize = titleFit ? titleFit.size : 0;
  const rowHeight = itemSize * 1.86;
  // The card has to fit the widest thing on it, title included - sizing from
  // the items alone is what let a long heading run off the edge.
  const widest = Math.max(
    0, ...fitted.map(entry => entry.width),
    titleFit ? titleFit.width - tick - 18 * u * scale : 0,
  );
  return {
    u, scale, items, fitted, title, titleFit, titleSize, itemSize, tick, rowHeight,
    width: Math.min(maxWidth, Math.max(320 * u * scale, widest + tick + 78 * u * scale)),
    height: titleSize * (title ? 1.9 : 0) + Math.max(1, items.length) * rowHeight
      + 44 * u * scale,
  };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const card = A.motion(instance, t, "card");
    const title = A.motion(instance, t, "title");
    const left = -g.width / 2 + 34 * g.scale * g.u;
    let cursor = -g.height / 2 + 30 * g.scale * g.u;

    A.stage(ctx, card, () => {
      A.shadow(ctx, card.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 13 * g.u));
      paperCard(ctx, A, { w: g.width, h: g.height, radius: 13, seed: instance.id, rule: 21 });
    });
    if (g.title) {
      A.stage(ctx, title, () => {
        drawLines(ctx, g.titleFit.lines, {
          size: g.titleSize, weight: 800, color: PALETTE.ink,
          align: "left", x: left, y: cursor + g.titleSize * 0.55,
        });
      });
      cursor += g.titleSize * 1.9;
    }
    g.items.forEach((item, index) => {
      const state = A.motion(instance, t, `item${index}`);
      const y = cursor + g.rowHeight * (index + 0.5) - g.rowHeight * 0.5 + g.itemSize * 0.9;
      A.stage(ctx, state, () => {
        ctx.save();
        ctx.translate(left + g.tick / 2, y - g.itemSize * 0.06);
        checkMark(ctx, {
          size: g.tick, progress: A.value(instance, t, `item${index}`),
          color: A.categoryColor("checklist"), width: Math.max(2, g.itemSize * 0.16),
        });
        ctx.restore();
        drawLines(ctx, g.fitted[index].lines, {
          size: g.fitted[index].size, weight: 600, color: PALETTE.ink,
          align: "left", x: left + g.tick + 18 * g.scale * g.u, y,
        });
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
