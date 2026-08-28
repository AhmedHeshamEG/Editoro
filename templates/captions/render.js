import { roundRectPath, wrapLines, applyDirection, isRTL, alpha } from "/tpl/_shared/kit.js";

const ID = "captions";

function metrics(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const style = instance.fields.style || {};
  const placement = instance.fields.layout || {};
  const scale = (placement.scale ?? 1) * (instance.scale ?? 1);
  const size = (style.font_size || 44) * scale * u;
  const maxWidth = viewport.width * (placement.max_width ?? 0.84);
  ctx.font = `700 ${size}px Rubik`;
  const lines = wrapLines(ctx, instance.fields.text || "", maxWidth, 3);
  const widest = Math.max(0, ...lines.map(line => ctx.measureText(line).width));
  const step = size * 1.20;
  const padX = 26 * scale * u, padY = 15 * scale * u;
  return {
    u, style, placement, scale, size, lines, step, padX, padY,
    width: Math.min(maxWidth + padX * 2, widest + padX * 2),
    height: lines.length * step + padY * 2,
  };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const m = metrics(instance, A, ctx.canvas, ctx);
    const state = A.motion(instance, t);
    const words = instance.fields.words || [];
    const highlight = m.style.highlight_active_word !== false && words.length > 0;
    const localTime = instance.start + t * instance.duration;
    const align = m.placement.align || "center";

    A.stage(ctx, state, () => {
      if (m.style.box !== false) {
        roundRectPath(ctx, -m.width / 2, -m.height / 2, m.width, m.height, 12 * m.u);
        ctx.fillStyle = "#0b0c0ec4";
        ctx.fill();
      }
      ctx.font = `700 ${m.size}px Rubik`;
      ctx.textBaseline = "middle";
      m.lines.forEach((line, index) => {
        const y = (index - (m.lines.length - 1) / 2) * m.step;
        applyDirection(ctx, line);
        if (!highlight) {
          ctx.textAlign = align === "center" ? "center" : (isRTL(line) ? "right" : "left");
          const x = align === "center" ? 0
            : (isRTL(line) ? m.width / 2 - m.padX : -m.width / 2 + m.padX);
          ctx.lineJoin = "round";
          ctx.lineWidth = m.size * 0.14;
          ctx.strokeStyle = "#000000d0";
          ctx.strokeText(line, x, y);
          ctx.fillStyle = "#f4f2ee";
          ctx.fillText(line, x, y);
          return;
        }
        // Word-level: draw each token separately so the one being spoken can
        // lift. Whisper already produced these timings; using them costs
        // nothing and is the difference between captions and a teleprompter.
        const tokens = line.split(/(\s+)/).filter(part => part.length);
        const widths = tokens.map(token => ctx.measureText(token).width);
        const lineWidth = widths.reduce((sum, value) => sum + value, 0);
        let cursor = align === "center" ? -lineWidth / 2
          : (isRTL(line) ? m.width / 2 - m.padX - lineWidth : -m.width / 2 + m.padX);
        ctx.textAlign = "left";
        ctx.direction = "ltr";
        let wordIndex = m.lines.slice(0, index).join(" ").trim().split(/\s+/).filter(Boolean).length;
        tokens.forEach((token, tokenIndex) => {
          if (!token.trim()) { cursor += widths[tokenIndex]; return; }
          const timing = words[wordIndex];
          const active = timing && localTime >= timing.s && localTime < timing.e;
          ctx.save();
          ctx.lineJoin = "round";
          ctx.lineWidth = m.size * 0.14;
          ctx.strokeStyle = "#000000d0";
          ctx.strokeText(token, cursor, y);
          ctx.fillStyle = active ? (m.style.highlight_color || "#FFD166") : "#f4f2ee";
          if (active) {
            ctx.shadowColor = alpha(m.style.highlight_color || "#FFD166", 0.55);
            ctx.shadowBlur = m.size * 0.35;
          }
          ctx.fillText(token, cursor, y);
          ctx.restore();
          cursor += widths[tokenIndex];
          wordIndex += 1;
        });
      });
    });
  },
  measure(instance, A, viewport) {
    const probe = document.createElement("canvas").getContext("2d");
    probe.canvas.width = viewport.width; probe.canvas.height = viewport.height;
    const m = metrics(instance, A, viewport, probe);
    return { width: m.width, height: m.height };
  },
});
