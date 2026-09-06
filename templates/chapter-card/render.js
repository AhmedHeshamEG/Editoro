import { fitText, drawLines, PALETTE, alpha } from "/tpl/_shared/kit.js";

const ID = "chapter-card";

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const accent = A.categoryColor(ID);
    const vertical = A.orientation() === "vertical";
    const bandHeight = H * (vertical ? 0.30 : 0.34);
    const bandTop = H * (vertical ? 0.34 : 0.33);

    const band = A.motion(instance, t, "band");
    A.stage(ctx, band, () => {
      ctx.fillStyle = "#0b0c0fe8";
      ctx.fillRect(0, bandTop, W, bandHeight);
      ctx.fillStyle = accent;
      ctx.fillRect(0, bandTop, W, 5 * u);
      ctx.fillStyle = alpha(accent, 0.55);
      ctx.fillRect(0, bandTop + bandHeight - 3 * u, W, 3 * u);
    });

    const left = W * (vertical ? 0.09 : 0.14);
    const number = String(instance.fields.number || "").trim();
    const title = String(instance.fields.title || "").trim();
    const subtitle = String(instance.fields.subtitle || "").trim();
    const titleSize = (vertical ? 74 : 92) * u * instance.scale;
    let cursor = bandTop + bandHeight / 2;

    if (number) {
      const state = A.motion(instance, t, "number");
      A.stage(ctx, state, () => {
        drawLines(ctx, [number], {
          size: titleSize * 0.34, weight: 800, family: "Inter", color: accent,
          tracking: titleSize * 0.05, align: "left",
          x: left, y: cursor - titleSize * (subtitle ? 0.92 : 0.72),
        });
      });
    }
    if (title) {
      const state = A.motion(instance, t, "title");
      A.stage(ctx, state, () => {
        const fitted = fitText(ctx, title, {
          maxWidth: W - left * 2, maxLines: 2, weight: 800,
          size: titleSize, minSize: 20 * u * scale,
        });
        drawLines(ctx, fitted.lines, {
          size: fitted.size, weight: 800, color: "#f6f4ef", align: "left",
          lineHeight: 1.06, x: left,
          y: cursor - (subtitle ? titleSize * 0.16 : 0) + (number ? titleSize * 0.12 : 0),
        });
      });
    }
    if (subtitle) {
      const state = A.motion(instance, t, "subtitle");
      A.stage(ctx, state, () => {
        const fitted = fitText(ctx, subtitle, {
          maxWidth: W - left * 2, maxLines: 1, weight: 500,
          size: titleSize * 0.32, minSize: 12 * u * scale,
        });
        drawLines(ctx, fitted.lines, {
          size: fitted.size, weight: 500, color: alpha(PALETTE.inkFaint, 0.95),
          align: "left", x: left, y: cursor + titleSize * 0.68,
        });
      });
    }
    void cursor;
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height * 0.34 };
  },
});
