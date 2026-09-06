const ID = "backdrop-blur";

// This template deliberately paints nothing. Its entire effect is the blur the
// compositor applies underneath it, and the strength of that blur is this
// block's own motion opacity - which is why it needs a `draw` at all: the
// renderer only asks for motion state on blocks it is drawing. Painting a
// fully transparent frame keeps it in that path without putting a pixel on
// screen, so the fade in and out come from the same curve every other template
// uses rather than from a second animation system.
registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    A.motion(instance, t);
  },
  measure(instance, A, viewport) {
    return { width: viewport.width, height: viewport.height };
  },
});
