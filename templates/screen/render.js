const templateId = "screen";

function geometry(instance, assets, viewport) {
  const image = instance.fields.asset && assets.load(assets.projectAsset(instance.fields.asset));
  const ready = image?.complete && image.naturalWidth;
  const imageWidth = ready ? image.naturalWidth : 640;
  const imageHeight = ready ? image.naturalHeight : 400;
  const unit = assets.viewportUnit(viewport);
  const width = Math.min(680 * unit, imageWidth * unit) * instance.scale;
  return {image, ready, unit, w: width, h: width * imageHeight / imageWidth};
}

registerTemplate(templateId, {
  draw(ctx, instance, t, assets) {
    const envelope = assets.envelope(instance, t);
    const shape = geometry(instance, assets, ctx.canvas);
    const scale = envelope.phase === "in" ? 0.96 + 0.04 * assets.easeOut(envelope.k)
      : envelope.phase === "out" ? assets.easeOut(envelope.k) : 1;
    ctx.save();
    ctx.globalAlpha = envelope.phase === "hold" ? 1 : envelope.k;
    ctx.scale(scale, scale);
    ctx.fillStyle = "#23252b";
    ctx.shadowColor = "#000a";
    ctx.shadowBlur = 24 * shape.unit;
    ctx.beginPath();
    ctx.roundRect(-shape.w / 2 - 10 * shape.unit, -shape.h / 2 - 34 * shape.unit,
      shape.w + 20 * shape.unit, shape.h + 44 * shape.unit, 12 * shape.unit);
    ctx.fill();
    ctx.shadowBlur = 0;
    ["#ff5f57", "#febc2e", "#28c840"].forEach((color, index) => {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(-shape.w / 2 + 8 * shape.unit + index * 18 * shape.unit,
        -shape.h / 2 - 22 * shape.unit, 5 * shape.unit, 0, Math.PI * 2);
      ctx.fill();
    });
    if (shape.ready) ctx.drawImage(shape.image, -shape.w / 2, -shape.h / 2, shape.w, shape.h);
    else {
      ctx.fillStyle = "#0006";
      ctx.fillRect(-shape.w / 2, -shape.h / 2, shape.w, shape.h);
    }
    const frame = assets.load(assets.templateAsset(templateId, "frame.png"));
    if (frame?.complete && frame.naturalWidth) {
      ctx.drawImage(frame, -shape.w / 2 - 36 * shape.unit, -shape.h / 2 - 60 * shape.unit,
        shape.w + 72 * shape.unit, shape.h + 94 * shape.unit);
    }
    ctx.restore();
  },
  measure(instance, assets, viewport) {
    const shape = geometry(instance, assets, viewport);
    return {width: shape.w + 72 * shape.unit, height: shape.h + 94 * shape.unit};
  },
});
