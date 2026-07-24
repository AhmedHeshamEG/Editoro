const templateId = "image-pop";

function paperFrame(ctx, width, height, seed, assets) {
  const unit = assets.unit(ctx);
  const texture = assets.load(assets.templateAsset(templateId, "paper-texture.png"));
  ctx.fillStyle = texture?.complete && texture.naturalWidth ? ctx.createPattern(texture, "repeat") : "#faf6ec";
  ctx.shadowColor = "#0008";
  ctx.shadowBlur = 20 * unit;
  ctx.shadowOffsetY = 7 * unit;
  ctx.beginPath();
  ctx.roundRect(-width / 2, -height / 2, width, height, 8 * unit);
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.strokeStyle = "#0002";
  ctx.stroke();
}

function geometry(instance, assets, viewport) {
  const url = assets.instanceAssetUrl(instance);
  const image = url && assets.load(url);
  const ready = image?.complete && image.naturalWidth;
  const width = ready ? image.naturalWidth : 480;
  const height = ready ? image.naturalHeight : 300;
  const unit = assets.viewportUnit(viewport);
  const fitted = assets.fitContain(width, height, 560 * instance.scale * unit, 420 * instance.scale * unit);
  return {image, ready, unit, ...fitted};
}

registerTemplate(templateId, {
  draw(ctx, instance, t, assets) {
    const envelope = assets.envelope(instance, t);
    const shape = geometry(instance, assets, ctx.canvas);
    const scale = envelope.phase === "in"
      ? assets.easeOutBack(envelope.k)
      : envelope.phase === "out" ? assets.easeOut(envelope.k) : 1;
    const offsetY = envelope.phase === "in" ? (1 - assets.easeOut(envelope.k)) * -60 * shape.unit : 0;
    ctx.save();
    ctx.translate(0, offsetY);
    ctx.scale(scale, scale);
    ctx.rotate(assets.seeded(instance.id)() * 0.06 - 0.03);
    paperFrame(ctx, shape.w + 34 * shape.unit, shape.h + 58 * shape.unit, instance.id, assets);
    if (shape.ready) ctx.drawImage(shape.image, -shape.w / 2, -shape.h / 2 - 10 * shape.unit, shape.w, shape.h);
    else {
      ctx.fillStyle = "#0002";
      ctx.fillRect(-shape.w / 2, -shape.h / 2 - 10 * shape.unit, shape.w, shape.h);
    }
    const frame = assets.load(assets.templateAsset(templateId, "frame.png"));
    if (frame?.complete && frame.naturalWidth) {
      ctx.drawImage(frame, -shape.w / 2 - 30 * shape.unit, -shape.h / 2 - 40 * shape.unit,
        shape.w + 60 * shape.unit, shape.h + 100 * shape.unit);
    }
    ctx.restore();
  },
  measure(instance, assets, viewport) {
    const shape = geometry(instance, assets, viewport);
    return {width: shape.w + 60 * shape.unit, height: shape.h + 100 * shape.unit};
  },
});
