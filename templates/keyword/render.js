const templateId = "keyword";

function wrapText(ctx, text, maxWidth) {
  const words = String(text || "…").trim().split(/\s+/).filter(Boolean);
  const lines = [];
  let line = "";
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (line && ctx.measureText(candidate).width > maxWidth) {
      lines.push(line);
      line = word;
      if (lines.length === 2) break;
    } else {
      line = candidate;
    }
  }
  if (lines.length < 2 && line) lines.push(line);
  if (!lines.length) lines.push("…");
  if (words.join(" ") !== lines.join(" ")) {
    while (lines[1] && ctx.measureText(`${lines[1]}…`).width > maxWidth) {
      lines[1] = lines[1].slice(0, -1);
    }
    lines[1] = `${lines[1] || ""}…`;
  }
  return lines.slice(0, 2);
}

function drawPaper(ctx, width, height, seed, assets) {
  const unit = assets.unit(ctx);
  const texture = assets.load(assets.templateAsset(templateId, "paper-texture.png"));
  ctx.fillStyle = texture?.complete && texture.naturalWidth
    ? ctx.createPattern(texture, "repeat")
    : "#faf6ec";
  ctx.shadowColor = "#00000066";
  ctx.shadowBlur = 18 * unit;
  ctx.shadowOffsetY = 6 * unit;
  ctx.beginPath();
  ctx.roundRect(-width / 2, -height / 2, width, height, 9 * unit);
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.strokeStyle = "#00000022";
  ctx.stroke();
  const random = assets.seeded(seed);
  ctx.strokeStyle = "#8aa7c633";
  for (let y = -height / 2 + 22 * unit; y < height / 2 - 6 * unit; y += 18 * unit) {
    ctx.beginPath();
    ctx.moveTo(-width / 2 + 9 * unit, y + random() * unit);
    ctx.lineTo(width / 2 - 9 * unit, y + random() * unit);
    ctx.stroke();
  }
}

registerTemplate(templateId, {
  draw(ctx, instance, t, assets) {
    const envelope = assets.envelope(instance, t);
    const unit = assets.unit(ctx);
    const fontSize = 58 * instance.scale * unit;
    ctx.save();
    ctx.font = `800 ${fontSize}px Rubik`;
    const maxTextWidth = ctx.canvas.width * 0.62;
    const lines = wrapText(ctx, instance.fields.text, maxTextWidth);
    const textWidth = Math.max(...lines.map(line => ctx.measureText(line).width));
    const width = Math.min(ctx.canvas.width * 0.72, Math.max(220 * unit, textWidth + 72 * unit));
    const height = (lines.length === 1 ? 105 : 170) * instance.scale * unit;
    const scale = envelope.phase === "in"
      ? assets.easeOutBack(envelope.k)
      : envelope.phase === "out" ? assets.easeOut(envelope.k) : 1;
    ctx.scale(scale, scale);
    ctx.rotate(-0.018);
    drawPaper(ctx, width, height, instance.id, assets);
    ctx.fillStyle = "#1c1c1c";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const lineHeight = fontSize * 1.08;
    lines.forEach((line, index) => {
      ctx.fillText(line, 0, (index - (lines.length - 1) / 2) * lineHeight + 3 * unit);
    });
    ctx.restore();
  },
  measure(instance, assets, viewport) {
    const unit = assets.viewportUnit(viewport);
    const twoLines = String(instance.fields.text || "").length > 26;
    return {
      width: Math.min(viewport.width * 0.72, Math.max(220 * unit, String(instance.fields.text || "").length * 32 * instance.scale * unit)),
      height: (twoLines ? 170 : 105) * instance.scale * unit,
    };
  },
});
