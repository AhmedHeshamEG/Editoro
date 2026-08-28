import {
  darkPanel, roundRectPath, fitText, drawLines, PALETTE, alpha, fitCover,
} from "/tpl/_shared/kit.js";

const ID = "chat-bubble";

function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const width = viewport.width * (variant.max_width || 0.44);
  const pad = 30 * scale * u;
  const avatar = 62 * scale * u;
  const nameSize = 30 * scale * u;
  const body = fitText(ctx, instance.fields.text || "", {
    maxWidth: width - pad * 2, maxLines: 6, weight: 500,
    size: 32 * scale * u, minSize: 12 * u,
  });
  const bodyHeight = body.lines.length * body.size * 1.34;
  return {
    u, scale, width, pad, avatar, nameSize, body, bodyHeight,
    height: pad * 2 + avatar + 20 * scale * u + bodyHeight,
  };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const light = (instance.fields.theme || "dark") === "light";
    const surface = light ? "#ffffff" : "#181b22";
    const ink = light ? PALETTE.ink : "#eceef3";
    const faint = light ? PALETTE.inkFaint : "#8b909c";
    const card = A.motion(instance, t, "card");
    const head = A.motion(instance, t, "head");
    const body = A.motion(instance, t, "body");
    const left = -g.width / 2 + g.pad;
    const top = -g.height / 2 + g.pad;

    A.stage(ctx, card, () => {
      A.shadow(ctx, card.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 20 * g.u));
      darkPanel(ctx, A, {
        w: g.width, h: g.height, radius: 20, fill: surface,
        border: light ? "#00000018" : "#ffffff16",
      });
    });
    A.stage(ctx, head, () => {
      const cx = left + g.avatar / 2, cy = top + g.avatar / 2;
      const image = instance.fields.avatar
        ? A.load(A.projectAsset(instance.fields.avatar)) : null;
      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, g.avatar / 2, 0, 7);
      ctx.clip();
      if (image?.complete && image.naturalWidth) {
        const fitted = fitCover(image.naturalWidth, image.naturalHeight, g.avatar, g.avatar);
        ctx.drawImage(image, cx - fitted.w / 2, cy - fitted.h / 2, fitted.w, fitted.h);
      } else {
        ctx.fillStyle = A.categoryColor(ID);
        ctx.fillRect(cx - g.avatar, cy - g.avatar, g.avatar * 2, g.avatar * 2);
        const initial = String(instance.fields.name || "?").trim().charAt(0).toUpperCase() || "?";
        drawLines(ctx, [initial], {
          size: g.avatar * 0.5, weight: 800, color: "#101318", x: cx, y: cy,
        });
      }
      ctx.restore();
      const name = String(instance.fields.name || "").trim();
      const handle = String(instance.fields.handle || "").trim();
      const textLeft = left + g.avatar + 18 * g.scale * g.u;
      if (name) {
        const fitted = fitText(ctx, name, {
          maxWidth: g.width - (textLeft + g.width / 2) - g.pad, maxLines: 1,
          weight: 800, family: "Inter", size: g.nameSize, minSize: 11 * g.u,
        });
        drawLines(ctx, fitted.lines, {
          size: fitted.size, weight: 800, family: "Inter", color: ink,
          align: "left", x: textLeft, y: cy - (handle ? g.nameSize * 0.6 : 0),
        });
      }
      if (handle) {
        drawLines(ctx, [handle], {
          size: g.nameSize * 0.78, weight: 500, family: "Inter",
          color: alpha(faint, 0.95), align: "left",
          x: textLeft, y: cy + g.nameSize * 0.62,
        });
      }
    });
    A.stage(ctx, body, () => {
      drawLines(ctx, g.body.lines, {
        size: g.body.size, weight: 500, lineHeight: 1.34, color: ink, align: "left",
        x: left, y: top + g.avatar + 20 * g.scale * g.u + g.bodyHeight / 2,
      });
    });
  },
  measure(instance, A, viewport) {
    const probe = document.createElement("canvas").getContext("2d");
    probe.canvas.width = viewport.width; probe.canvas.height = viewport.height;
    const g = layout(instance, A, viewport, probe);
    return { width: g.width, height: g.height };
  },
});
