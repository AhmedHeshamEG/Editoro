/* Draws nothing on purpose. The block exists so that a stretch of the video can
   breathe at its own strength, and that strength is read straight off the
   instance by breatheAmplitude() in the engine and by breathe_spans() in
   server.py. Giving it a picture would mean putting a graphic on screen to
   describe a movement, which is what the timeline block is already for. */
registerTemplate("breathe", {
  draw() {},
  measure(instance, A, viewport) {
    return { width: viewport.width * 0.2, height: viewport.height * 0.2 };
  },
});
