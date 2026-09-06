import { fitText, drawLines, highlighterSweep, alpha } from "/tpl/_shared/kit.js";

const ID = "hook-card";

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const vertical = A.orientation() === "vertical";
    const maxWidth = W * (vertical ? 0.86 : 0.72);
    // Full-frame packs are handed an untranslated context, so centre it once
    // here rather than adding W / 2 to every coordinate below.
    ctx.translate(W / 2, 0);

    const scrim = A.motion(instance, t, "scrim");
    A.stage(ctx, scrim, () => {
      const gradient = ctx.createLinearGradient(0, 0, 0, H);
      gradient.addColorStop(0, "#05060899");
      gradient.addColorStop(0.5, "#050608cc");
      gradient.addColorStop(1, "#05060899");
      ctx.fillStyle = gradient;
      ctx.fillRect(-W / 2, 0, W, H);
    });

    const kicker = String(instance.fields.kicker || "").trim();
    const centre = H * (vertical ? 0.46 : 0.48);
    if (kicker) {
      const state = A.motion(instance, t, "kicker");
      A.stage(ctx, state, () => {
        drawLines(ctx, [kicker.toUpperCase()], {
          size: 30 * u, weight: 800, family: "Inter",
          color: A.categoryColor(ID), tracking: 30 * u * 0.18,
          y: centre - (vertical ? 190 : 150) * u,
        });
      });
    }

    const state = A.motion(instance, t, "text");
    const fitted = fitText(ctx, instance.fields.text || "…", {
      maxWidth, maxLines: vertical ? 4 : 3, weight: 800,
      size: (vertical ? 92 : 104) * u * instance.scale, minSize: 22 * u * scale,
    });
    A.stage(ctx, state, () => {
      drawLines(ctx, fitted.lines, {
        size: fitted.size, weight: 800, color: "#f7f5f0", lineHeight: 1.10,
        y: centre, stroke: alpha("#000000", 0.55), strokeWidth: fitted.size * 0.10,
      });
    });

    const underline = A.motion(instance, t, "underline");
    const bodyHeight = fitted.lines.length * fitted.size * 1.10;
    A.stage(ctx, underline, () => {
      const width = Math.min(maxWidth, fitted.width * 1.02);
      highlighterSweep(ctx, A, {
        x: -width / 2, y: centre + bodyHeight / 2 + fitted.size * 0.18,
        w: width, h: fitted.size * 0.30,
        progress: A.value(instance, t, "underline"),
        color: A.categoryColor(ID), opacity: 0.7,
      });
    });
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
