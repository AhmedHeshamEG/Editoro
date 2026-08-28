import { art } from "/tpl/_shared/kit.js";

registerTemplate("ambient", {
  draw(ctx, instance, t, A) {
    const W = ctx.canvas.width, H = ctx.canvas.height;
    const state = A.motion(instance, t);
    const random = A.seeded(instance.id);
    const intensity = Math.max(0.05, Math.min(1, instance.fields.intensity ?? 0.35));
    const seconds = state.seconds;
    ctx.save();
    ctx.globalAlpha = state.opacity * intensity;
    const field = A.load(art("ambient-field.png"));
    if (field?.complete && field.naturalWidth) {
      // Two counter-drifting copies: one texture sliding on its own reads as a
      // pan, two at different rates read as light moving over a still surface.
      for (const [speed, weight] of [[0.010, 0.62], [-0.006, 0.38]]) {
        const driftX = Math.sin(seconds * 0.11 + speed * 90) * W * speed * 4;
        const driftY = Math.cos(seconds * 0.08 + speed * 60) * H * speed * 3;
        ctx.globalAlpha = state.opacity * intensity * weight;
        ctx.drawImage(field, -W * 0.02 + driftX, -H * 0.02 + driftY, W * 1.04, H * 1.04);
      }
    }
    ctx.globalCompositeOperation = "screen";
    for (let pool = 0; pool < 5; pool++) {
      const phase = random() * 6.283, speed = 0.03 + random() * 0.04;
      const x = W * (0.18 + 0.64 * random()) + Math.sin(seconds * speed + phase) * W * 0.06;
      const y = H * (0.18 + 0.64 * random()) + Math.cos(seconds * speed * 0.8 + phase) * H * 0.06;
      const gradient = ctx.createRadialGradient(x, y, 0, x, y, H * 0.30);
      gradient.addColorStop(0, "#ffffff");
      gradient.addColorStop(1, "#ffffff00");
      ctx.globalAlpha = state.opacity * intensity * 0.16;
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, W, H);
    }
    ctx.restore();
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
