import {
  darkPanel, roundRectPath, fitContain, placeholder, fitText, drawLines, PALETTE,
} from "/tpl/_shared/kit.js";

const ID = "screen";
const LIGHTS = ["#ff5f57", "#febc2e", "#28c840"];

function layout(instance, A, viewport) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const image = A.load(A.instanceAssetUrl(instance));
  const ready = Boolean(image?.complete && image.naturalWidth);
  const chrome = instance.fields.chrome || "window";
  const bar = chrome === "plain" ? 0 : (chrome === "browser" ? 52 : 40) * scale * u;
  const box = fitContain(
    ready ? image.naturalWidth : 16,
    ready ? image.naturalHeight : 10,
    viewport.width * (variant.max_width || 0.5) * scale,
    viewport.height * (variant.max_height || 0.62) * scale - bar,
  );
  const pad = 10 * scale * u;
  return { u, scale, image, ready, chrome, bar, box, pad,
    width: box.w + pad * 2, height: box.h + bar + pad * 2 };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas);
    const shell = A.motion(instance, t, "window");
    const content = A.motion(instance, t, "content");
    const chrome = A.motion(instance, t, "chrome");
    const top = -g.height / 2;

    A.stage(ctx, shell, () => {
      A.shadow(ctx, shell.shadowSpec,
        c => roundRectPath(c, -g.width / 2, top, g.width, g.height, 13 * g.u));
      darkPanel(ctx, A, { w: g.width, h: g.height, radius: 13 });
    });
    A.stage(ctx, content, () => {
      ctx.save();
      roundRectPath(ctx, -g.box.w / 2, top + g.bar + g.pad, g.box.w, g.box.h, 5 * g.u);
      ctx.clip();
      if (g.ready) ctx.drawImage(g.image, -g.box.w / 2, top + g.bar + g.pad, g.box.w, g.box.h);
      else {
        ctx.translate(0, top + g.bar + g.pad + g.box.h / 2);
        placeholder(ctx, A, { w: g.box.w, h: g.box.h, label: "SCREENSHOT" });
      }
      ctx.restore();
    });
    if (g.chrome === "plain") return;
    A.stage(ctx, chrome, () => {
      const barCentre = top + g.bar / 2 + g.pad * 0.2;
      ctx.strokeStyle = "#ffffff12";
      ctx.lineWidth = Math.max(1, g.u);
      ctx.beginPath();
      ctx.moveTo(-g.width / 2 + 4 * g.u, top + g.bar);
      ctx.lineTo(g.width / 2 - 4 * g.u, top + g.bar);
      ctx.stroke();
      LIGHTS.forEach((colour, index) => {
        ctx.fillStyle = colour;
        ctx.beginPath();
        ctx.arc(-g.width / 2 + (20 + index * 20) * g.scale * g.u, barCentre,
          5.5 * g.scale * g.u, 0, 7);
        ctx.fill();
      });
      const title = String(instance.fields.title || "").trim();
      if (!title) return;
      if (g.chrome === "browser") {
        const fieldWidth = g.width - 130 * g.scale * g.u;
        roundRectPath(ctx, -fieldWidth / 2 + 24 * g.scale * g.u, barCentre - 13 * g.scale * g.u,
          fieldWidth, 26 * g.scale * g.u, 13 * g.scale * g.u);
        ctx.fillStyle = "#00000055";
        ctx.fill();
      }
      const fitted = fitText(ctx, title, {
        maxWidth: g.width - 150 * g.scale * g.u, maxLines: 1, weight: 600,
        family: "Inter", size: 19 * g.scale * g.u, minSize: 9 * g.u,
      });
      drawLines(ctx, fitted.lines, {
        size: fitted.size, weight: 600, family: "Inter",
        color: "#cfd2da", y: barCentre + 1 * g.u,
        x: g.chrome === "browser" ? 12 * g.scale * g.u : 0,
      });
      void PALETTE;
    });
  },
  measure(instance, A, viewport) {
    const g = layout(instance, A, viewport);
    return { width: g.width, height: g.height };
  },
});
