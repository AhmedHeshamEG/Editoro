import { fitText, drawLines } from "/tpl/_shared/kit.js";

const ID = "frame-stamp";

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const u = A.unit(ctx);
    const accent = A.categoryColor(ID);
    const flash = Math.max(0, Math.min(1, instance.fields.flash ?? 0.5));
    const flashWindow = 3 / A.fps() / Math.max(0.001, instance.duration);
    if (flash > 0 && t < flashWindow) {
      ctx.save();
      ctx.globalAlpha = flash * (1 - t / flashWindow);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, W, H);
      ctx.restore();
    }
    const marks = A.motion(instance, t, "marks");
    const inset = 46 * u, arm = 62 * u;
    A.stage(ctx, marks, () => {
      ctx.strokeStyle = accent;
      ctx.lineWidth = Math.max(2, 5 * u);
      ctx.lineCap = "square";
      for (const [cx, cy, dx, dy] of [
        [inset, inset, 1, 1], [W - inset, inset, -1, 1],
        [inset, H - inset, 1, -1], [W - inset, H - inset, -1, -1],
      ]) {
        ctx.beginPath();
        ctx.moveTo(cx, cy + dy * arm);
        ctx.lineTo(cx, cy);
        ctx.lineTo(cx + dx * arm, cy);
        ctx.stroke();
      }
    });
    const label = String(instance.fields.label || "").trim();
    if (!label) return;
    const state = A.motion(instance, t, "label");
    A.stage(ctx, state, () => {
      const fitted = fitText(ctx, label.toUpperCase(), {
        maxWidth: W * 0.5, maxLines: 1, weight: 800, family: "Inter",
        size: 30 * u, minSize: 11 * u * scale,
      });
      const padX = 20 * u, padY = 11 * u;
      const boxWidth = fitted.width + padX * 2, boxHeight = fitted.size + padY * 2;
      const x = inset, y = H - inset - arm - boxHeight - 14 * u;
      ctx.fillStyle = accent;
      ctx.fillRect(x, y, boxWidth, boxHeight);
      drawLines(ctx, fitted.lines, {
        size: fitted.size, weight: 800, family: "Inter", color: "#08121a",
        tracking: fitted.size * 0.12, align: "left",
        x: x + padX, y: y + boxHeight / 2,
      });
    });
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
