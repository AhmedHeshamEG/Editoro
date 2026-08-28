/* Camera templates transform the source footage rather than drawing over it.
   The preview applies A.motion() through cameraTransform(); the export builds
   the matching FFmpeg zoompan graph from the same `camera` block. */
registerTemplate("punch-in", {
  draw() {},
  measure(instance, A, viewport) {
    const rect = instance.fields.rect || { w: 0.5, h: 0.5 };
    return { width: rect.w * viewport.width, height: rect.h * viewport.height };
  },
});
