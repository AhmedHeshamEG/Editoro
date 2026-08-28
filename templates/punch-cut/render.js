registerTemplate("punch-cut", {
  draw(ctx, instance, t, A) {
    const flash = Math.max(0, Math.min(0.6, instance.fields.flash ?? 0));
    if (flash <= 0) return;
    // Two frames of white, no more: a flash you can read as a flash has already
    // outstayed its welcome.
    const window = 2 / A.fps() / Math.max(0.001, instance.duration);
    if (t > window) return;
    ctx.save();
    ctx.globalAlpha = flash * (1 - t / window);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, ctx.canvas.width, ctx.canvas.height);
    ctx.restore();
  },
  measure(instance, A, viewport) {
    const rect = instance.fields.rect || { w: 0.5, h: 0.5 };
    return { width: rect.w * viewport.width, height: rect.h * viewport.height };
  },
});
