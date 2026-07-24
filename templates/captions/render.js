const templateId = "captions";

function wrap(ctx, text, maxWidth) {
  const words = String(text || "").trim().split(/\s+/).filter(Boolean);
  const lines = [];
  let line = "";
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (line && ctx.measureText(candidate).width > maxWidth) {
      lines.push(line);
      line = word;
    } else line = candidate;
  }
  if (line) lines.push(line);
  return lines.slice(0, 2);
}

registerTemplate(templateId, {
  draw(ctx, instance, _t, assets) {
    const style = instance.fields.style || {};
    const height = ctx.canvas.height;
    const unit = assets.viewportUnit(ctx.canvas);
    const fontSize = (style.size || 42) * unit;
    ctx.save();
    ctx.font = `700 ${fontSize}px Rubik`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const lines = wrap(ctx, instance.fields.text, ctx.canvas.width * 0.82);
    const lineHeight = fontSize * 1.16;
    const textWidth = Math.max(1, ...lines.map(line => ctx.measureText(line).width));
    const paddingX = 18 * unit;
    const paddingY = 11 * unit;
    const blockHeight = Math.max(lineHeight, lines.length * lineHeight) + paddingY * 2;
    const centerY = height * (style.y ?? instance.y ?? 0.88);
    ctx.fillStyle = style.bg || "#000000aa";
    ctx.beginPath();
    ctx.roundRect(ctx.canvas.width / 2 - textWidth / 2 - paddingX,
      centerY - blockHeight / 2, textWidth + paddingX * 2, blockHeight, 10 * unit);
    ctx.fill();
    ctx.fillStyle = style.color || "#F2F2F2";
    lines.forEach((line, index) => {
      ctx.fillText(line, ctx.canvas.width / 2,
        centerY + (index - (lines.length - 1) / 2) * lineHeight);
    });
    ctx.restore();
  },
});
