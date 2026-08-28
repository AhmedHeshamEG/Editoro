registerTemplate("ken-burns", {
  draw() {},
  measure(instance, A, viewport) {
    const rect = instance.fields.rect || { w: 0.5, h: 0.5 };
    return { width: rect.w * viewport.width, height: rect.h * viewport.height };
  },
});
