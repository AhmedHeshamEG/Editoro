registerTemplate("whip-pan", {
  draw(ctx, instance, t, A) {
    const strength = Math.max(0, Math.min(1, instance.fields.strength ?? 0.55));
    if (strength <= 0) return;
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const from = instance.fields.rect || { x: 0.06, w: 0.52 };
    const to = instance.fields.rect2 || { x: 0.42, w: 0.52 };
    const rightwards = (to.x + to.w / 2) >= (from.x + from.w / 2);
    // The streak peaks where the pan is fastest, which for outQuint is early.
    const speed = Math.sin(Math.PI * Math.min(1, Math.max(0, t))) ** 0.7;
    if (speed <= 0.02) return;
    const random = A.seeded(instance.id);
    ctx.save();
    ctx.globalAlpha = strength * speed * 0.72;
    ctx.globalCompositeOperation = "screen";
    for (let band = 0; band < 26; band++) {
      const y = random() * H;
      const height = H * (0.006 + random() * 0.035);
      const length = W * (0.25 + random() * 0.75) * speed;
      const x = rightwards ? W - length * (0.2 + random()) : random() * W * 0.4;
      const gradient = ctx.createLinearGradient(x, 0, x + length, 0);
      const shade = 150 + Math.round(random() * 90);
      gradient.addColorStop(0, `rgba(${shade},${shade},${shade},0)`);
      gradient.addColorStop(0.5, `rgba(${shade},${shade},${shade},0.30)`);
      gradient.addColorStop(1, `rgba(${shade},${shade},${shade},0)`);
      ctx.fillStyle = gradient;
      ctx.fillRect(x, y, length, height);
    }
    ctx.restore();
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
