const templateId = "zoom-out";

registerTemplate(templateId, {
  draw() {
    // Camera effects transform the source footage in the shared pipeline.
  },
  sourceTransform(instance, t, assets) {
    const rect = instance.fields.rect;
    if (!rect) return null;
    const envelope = assets.envelope(instance, t);
    const amount = envelope.phase === "in"
      ? 1 - assets.easeOut(envelope.k)
      : 0;
    const target = 1 / Math.max(Math.max(0.05, rect.w), Math.max(0.05, rect.h));
    return {
      cx: rect.x + rect.w / 2,
      cy: rect.y + rect.h / 2,
      scale: 1 + (target - 1) * amount,
    };
  },
});
