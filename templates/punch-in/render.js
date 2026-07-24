const templateId = "punch-in";

registerTemplate(templateId, {
  draw() {
    // The source footage is transformed by the shared preview/export pipeline.
  },
  sourceTransform(instance, t, assets) {
    const rect = instance.fields.rect;
    if (!rect) return null;
    const envelope = assets.envelope(instance, t);
    const amount = envelope.phase === "in"
      ? assets.easeOut(envelope.k)
      : envelope.phase === "out" ? assets.easeOut(envelope.k) : 1;
    const target = 1 / Math.max(Math.max(0.05, rect.w), Math.max(0.05, rect.h));
    return {
      cx: rect.x + rect.w / 2,
      cy: rect.y + rect.h / 2,
      scale: 1 + (target - 1) * amount,
    };
  },
});
