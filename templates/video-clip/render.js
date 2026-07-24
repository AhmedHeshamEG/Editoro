const templateId = "video-clip";

function geometry(instance, assets, viewport) {
  const video = instance.fields.asset && assets.video(assets.projectAsset(instance.fields.asset));
  const ready = video?.readyState >= 2;
  const width = ready ? video.videoWidth : 480;
  const height = ready ? video.videoHeight : 300;
  const unit = assets.viewportUnit(viewport);
  const fitted = assets.fitContain(width, height, 560 * instance.scale * unit, 420 * instance.scale * unit);
  return {video, ready, unit, ...fitted};
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
    ctx.rotate(assets.seeded(instance.id)() * 0.05 - 0.025);
    const texture = assets.load(assets.templateAsset(templateId, "paper-texture.png"));
    ctx.fillStyle = texture?.complete && texture.naturalWidth
      ? ctx.createPattern(texture, "repeat")
      : "#faf6ec";
    ctx.shadowColor = "#0008";
    ctx.shadowBlur = 20 * shape.unit;
    ctx.beginPath();
    ctx.roundRect(-shape.w / 2 - 17 * shape.unit, -shape.h / 2 - 29 * shape.unit,
      shape.w + 34 * shape.unit, shape.h + 58 * shape.unit, 8 * shape.unit);
    ctx.fill();
    ctx.shadowBlur = 0;
    if (shape.ready) ctx.drawImage(shape.video, -shape.w / 2, -shape.h / 2 - 10 * shape.unit, shape.w, shape.h);
    else {
      ctx.fillStyle = "#101216";
      ctx.fillRect(-shape.w / 2, -shape.h / 2 - 10 * shape.unit, shape.w, shape.h);
      ctx.fillStyle = "#6C9BFF";
      ctx.font = `600 ${22 * shape.unit}px Inter`;
      ctx.textAlign = "center";
      ctx.fillText("VIDEO", 0, 4 * shape.unit);
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
