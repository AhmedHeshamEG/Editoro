const templateId = "ambient";

registerTemplate(templateId, {
  draw(ctx, instance, t, assets) {
    const envelope = assets.envelope(instance, t);
    const random = assets.seeded(instance.id);
    const width = ctx.canvas.width;
    const height = ctx.canvas.height;
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = envelope.phase === "hold" ? 1 : assets.easeOut(envelope.k);
    const background = assets.load(assets.templateAsset(templateId, "ambient-paper.png"));
    if (background?.complete && background.naturalWidth) {
      const drift = Math.sin(t * Math.PI * 2) * width * 0.006;
      ctx.globalAlpha *= 0.30;
      ctx.drawImage(background, -width * 0.01 + drift, -height * 0.01, width * 1.02, height * 1.02);
    }
    ctx.globalAlpha = 0.06;
    for (let index = 0; index < 5; index++) {
      const phase = random() * Math.PI * 2;
      const speed = 0.05 + random() * 0.05;
      const x = width * (0.2 + 0.6 * random()) + Math.sin(t * Math.PI * 2 * speed + phase) * width * 0.05;
      const y = height * (0.2 + 0.6 * random()) + Math.cos(t * Math.PI * 2 * speed + phase) * height * 0.05;
      const gradient = ctx.createRadialGradient(x, y, 0, x, y, height * 0.25);
      gradient.addColorStop(0, "#ffffff");
      gradient.addColorStop(1, "#ffffff00");
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, width, height);
    }
    ctx.restore();
  },
  measure(_instance, _assets, viewport) {
    return {width: viewport.width, height: viewport.height};
  },
});
