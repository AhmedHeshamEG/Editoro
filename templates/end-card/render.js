import { fitText, drawLines, splitItems, roundRectPath, alpha } from "/tpl/_shared/kit.js";

const ID = "end-card";

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const accent = A.categoryColor(ID);
    const items = splitItems(instance.fields.items, 3);
    const maxWidth = W * (A.orientation() === "vertical" ? 0.84 : 0.62);
    // See hook-card: a full-frame pack has to centre itself.
    ctx.translate(W / 2, 0);

    const scrim = A.motion(instance, t, "scrim");
    A.stage(ctx, scrim, () => {
      ctx.fillStyle = "#05060ae0";
      ctx.fillRect(-W / 2, 0, W, H);
    });

    const titleFit = fitText(ctx, instance.fields.title || "…", {
      maxWidth, maxLines: 2, weight: 800, size: 84 * u * instance.scale, minSize: 20 * u,
    });
    const titleHeight = titleFit.lines.length * titleFit.size * 1.10;
    const rowHeight = 74 * u;
    const block = titleHeight + (items.length ? items.length * rowHeight + 40 * u : 0);
    const top = H / 2 - block / 2;

    const title = A.motion(instance, t, "title");
    A.stage(ctx, title, () => {
      drawLines(ctx, titleFit.lines, {
        size: titleFit.size, weight: 800, color: "#f7f5f0", lineHeight: 1.10,
        y: top + titleHeight / 2,
      });
    });
    items.forEach((item, index) => {
      const state = A.motion(instance, t, `item${index}`);
      const y = top + titleHeight + 40 * u + rowHeight * (index + 0.5);
      A.stage(ctx, state, () => {
        const fitted = fitText(ctx, item, {
          maxWidth: maxWidth - 96 * u, maxLines: 1, weight: 600,
          size: 36 * u, minSize: 12 * u,
        });
        const boxWidth = fitted.width + 96 * u;
        roundRectPath(ctx, -boxWidth / 2, y - rowHeight * 0.34, boxWidth, rowHeight * 0.68,
          rowHeight * 0.34);
        ctx.fillStyle = alpha(accent, 0.13);
        ctx.fill();
        ctx.strokeStyle = alpha(accent, 0.45);
        ctx.lineWidth = Math.max(1, 1.6 * u);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(-boxWidth / 2 + 32 * u, y, 7 * u, 0, 7);
        ctx.fillStyle = accent;
        ctx.fill();
        drawLines(ctx, fitted.lines, {
          size: fitted.size, weight: 600, color: "#eef0ee", align: "left",
          x: -boxWidth / 2 + 56 * u, y,
        });
      });
    });
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
