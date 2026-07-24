const templateId = "highlight";

function imageFor(instance, assets) {
  return assets.load(assets.instanceAssetUrl(instance));
}

function geometry(instance, assets, viewport) {
  const image = imageFor(instance, assets);
  const ready = image?.complete && image.naturalWidth;
  const imageWidth = ready ? image.naturalWidth : 600;
  const imageHeight = ready ? image.naturalHeight : 800;
  const unit = assets.viewportUnit(viewport);
  const width = Math.min(620 * unit, imageWidth * unit) * instance.scale;
  return {image, ready, unit, w: width, h: width * imageHeight / imageWidth};
}

registerTemplate(templateId, {
  draw(ctx, instance, t, assets) {
    const envelope = assets.envelope(instance, t);
    const shape = geometry(instance, assets, ctx.canvas);
    ctx.save();
    ctx.globalAlpha = envelope.phase === "hold" ? 1 : assets.easeOut(envelope.k);
    ctx.fillStyle = "#faf6ec";
    ctx.shadowColor = "#0008";
    ctx.shadowBlur = 18 * shape.unit;
    ctx.beginPath();
    ctx.roundRect(-shape.w / 2 - 15 * shape.unit, -shape.h / 2 - 15 * shape.unit,
      shape.w + 30 * shape.unit, shape.h + 30 * shape.unit, 7 * shape.unit);
    ctx.fill();
    ctx.shadowBlur = 0;
    if (shape.ready) ctx.drawImage(shape.image, -shape.w / 2, -shape.h / 2, shape.w, shape.h);
    const strokes = instance.fields.strokes || [];
    const progress = Math.min(1, Math.max(0, (t - 0.12) / 0.6));
    const texture = assets.load(assets.templateAsset(templateId, "stroke.png"));
    strokes.forEach((stroke, index) => {
      const amount = Math.min(1, Math.max(0, progress * strokes.length - index));
      if (amount <= 0) return;
      const x1 = -shape.w / 2 + stroke.x1 * shape.w;
      const x2 = -shape.w / 2 + stroke.x2 * shape.w;
      const y = -shape.h / 2 + stroke.y * shape.h;
      const width = (x2 - x1) * assets.easeOut(amount);
      const height = shape.h * 0.065;
      if (texture?.complete && texture.naturalWidth) {
        ctx.drawImage(texture, 0, 0, texture.naturalWidth * assets.easeOut(amount), texture.naturalHeight,
          x1, y - height / 2, width, height);
      } else {
        ctx.strokeStyle = `${assets.categoryColor(templateId)}99`;
        ctx.lineWidth = height;
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(x1, y);
        ctx.lineTo(x1 + width, y);
        ctx.stroke();
      }
    });
    ctx.restore();
  },
  measure(instance, assets, viewport) {
    const shape = geometry(instance, assets, viewport);
    return {width: shape.w + 30 * shape.unit, height: shape.h + 30 * shape.unit};
  },
  contentMeasure(instance, assets, viewport) {
    const shape = geometry(instance, assets, viewport);
    return {width: shape.w, height: shape.h};
  },
});
