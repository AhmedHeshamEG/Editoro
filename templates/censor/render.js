import { roundRectPath } from "/tpl/_shared/kit.js";

/* The overlay canvas is transparent and sits above the footage, so a cover has
   to be opaque rather than a filter over pixels it cannot read. A solid block
   with a pixel-grid texture reads as censorship and, unlike a real blur, cannot
   be undone by anyone who gets the file. */
registerTemplate("censor", {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const rect = instance.fields.rect || { x: 0.4, y: 0.4, w: 0.2, h: 0.14 };
    const x = rect.x * W, y = rect.y * H, w = rect.w * W, h = rect.h * H;
    const style = instance.fields.style || "pixelate";
    const random = A.seeded(instance.id);
    ctx.save();
    roundRectPath(ctx, x, y, w, h, style === "solid" ? 2 : 6 * A.unit(ctx));
    ctx.clip();
    ctx.fillStyle = "#14161b";
    ctx.fillRect(x, y, w, h);
    if (style !== "solid") {
      const cell = Math.max(6, Math.min(w, h) / 7);
      for (let cy = y; cy < y + h; cy += cell) {
        for (let cx = x; cx < x + w; cx += cell) {
          const shade = 26 + random() * 46;
          ctx.fillStyle = `rgb(${shade},${shade + 3},${shade + 8})`;
          ctx.fillRect(cx, cy, cell + 1, cell + 1);
        }
      }
      if (style === "blur") {
        ctx.filter = `blur(${cell * 0.55}px)`;
        ctx.drawImage(ctx.canvas, x, y, w, h, x, y, w, h);
        ctx.filter = "none";
      }
    }
    ctx.restore();
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
