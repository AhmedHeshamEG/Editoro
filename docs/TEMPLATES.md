# Writing a template pack

A template is a folder. Drop it in `templates/`, restart, and it appears in the
palette, in the MCP catalogue, in the directive grammar and in the export.

**Adding one never requires editing `server.py` or `index.html`.** If you find
yourself wanting to, the engine is missing a capability — add it there rather
than special-casing your pack.

```
templates/my-pack/
  template.json     what it is, what it takes, how it moves, how it sounds
  render.js         how it draws
  anything.png      assets only this pack uses (shared ones live in _shared/)
```

---

## template.json

```jsonc
{
  "id": "my-pack",                     // must match the folder name
  "version": 1,
  "schema": 2,
  "display_name": "اسم · My pack",      // shown in the palette
  "category": "text",                  // text|image|video|annotation|camera|screen|structure|ambient
  "category_color": "#7DD3A0",         // unique across all packs; identifies it everywhere
  "directive_verb": "اسم",              // optional: makes it placeable from a directive list
  "description": "One or two sentences saying what this is for and when to use it.",

  "fields": [
    {
      "name": "text",
      "label": "Headline",
      "type": "text",
      "max_length": 60,
      "default": "",
      "description": "What this field means. Required — it is what an agent reads."
    }
  ],

  "variants": {
    "base":       { "max_lines": 2 },
    "horizontal": { "x": 0.5, "y": 0.16, "scale": 1.0, "max_width": 0.60 },
    "vertical":   { "x": 0.5, "y": 0.19, "scale": 0.92, "max_width": 0.84 }
  },

  "motion": { /* see below */ },
  "sfx":    [ { "file": "_shared/sfx/pop.wav", "offset": 0.02, "gain": 0.5 } ],
  "assets": [],
  "default_duration": 2.5
}
```

Validation runs at startup. A pack that fails is skipped, named on the home
screen and reported by `/api/diagnostics` — it never half-loads.

### Rules the scanner enforces

- `id` matches the folder name and is lowercase kebab-case.
- `category_color` is unique across every pack. Colour is how you recognise a
  block before reading it, so two packs sharing one is a real conflict.
- **Both orientations are required.** A pack that only knows 16:9 would silently
  fall back to a 16:9 layout on a vertical cut.
- Every field needs a `description`. It is what a model reads to decide whether
  and how to use the template.
- `render.js` must exist. The engine has no built-in renderers.
- Referenced sounds and assets must exist.
- The `motion` block, if present, must be well-formed.

### Field types

| Type | Editor control | Value |
|---|---|---|
| `text` | single line | string |
| `textarea` | multi-line | string |
| `number` | slider, or a stepper with `"control": "integer"` | number |
| `select` | dropdown; needs `options` | string |
| `boolean` | toggle | bool |
| `color` | colour picker | `#RRGGBB` |
| `asset:image` | asset picker + upload | filename |
| `asset:video` | asset picker + upload | filename |
| `rect`, `rect2` | drag a rectangle on the preview | `{x, y, w, h}` in 0..1 |
| `strokes` | draw marker strokes on the preview | `[{x1, x2, y, h}]` in 0..1 |

A pack with any `asset:*` field is placed in a "missing asset" state until it is
filled — the block is drawn hatched on the timeline and dimmed on the preview,
which is how an incomplete edit stays visible.

### Variants

`base` is merged under both orientations. Beyond `x`, `y` and `scale`, put
whatever your renderer needs there — `max_width`, `max_lines`, `font`, `stack` —
and read it with `A.variant(id)`. That is how one renderer produces two genuinely
different layouts instead of a scaled-down copy of one.

---

## Motion

Animation is declared. The engine applies it identically in the preview and in
the export.

```jsonc
"motion": {
  "in": {
    "type": "spring",                  // or "ease"
    "stiffness": 220, "damping": 17,   // spring only
    "ms": 620,                         // window; the value is clamped to 1 after it
    "from": { "opacity": 0, "scale": 0.58, "y": -78, "rotate": -7 }
  },
  "out": {
    "type": "ease", "easing": "inCubic", "ms": 240,
    "to": { "opacity": 0, "scale": 0.92, "y": 14 }
  },
  "stagger": { "step_ms": 62, "elements": ["card", "text", "accent"] },
  "idle":    { "amplitude": 0.0028, "hz": 0.13 },
  "blur":    { "motion": true, "max": 7 },
  "shadow":  { "layers": 3, "elevation": 26, "opacity": 0.46, "lift": true }
}
```

- `from` / `to` offsets are in design units (1080p reference pixels); `rotate` is
  in degrees. They are interpolated to and from the identity transform.
- Springs are solved **analytically**, not integrated. The value at a given
  second is the same whether you scrub, play or render one isolated export
  frame — a stateful integrator could not promise that, and preview and export
  would drift apart.
- `stagger.elements` names the parts of your drawing. Element *i* is delayed by
  `i × step_ms` on the way in, and leaves in reverse order, so a card reads as
  one object rather than a pile.
- `idle` is the small drift a card keeps while it sits there. Keep it tiny; it is
  the difference between "resting" and "frozen".
- `blur.motion` samples real velocity and draws a directional smear. It costs a
  few extra draws during fast frames only.
- `shadow` is layered rather than a single offset blur, and `lift` adds a warm
  top edge and a cool bottom one so the sheet has thickness.

Easings available: `linear`, `inQuad`, `outQuad`, `inOutQuad`, `inCubic`,
`outCubic`, `inOutCubic`, `outQuart`, `outQuint`, `inExpo`, `outExpo`,
`inOutSine`, `outSine`, `inBack`, `outBack`, `outElastic`, `outBounce`.

### Camera templates

A pack that moves the *footage* instead of drawing over it declares a `camera`
block and draws nothing (or only an accent):

```jsonc
"camera": { "mode": "hold", "easing": "outCubic" }
```

| Mode | Behaviour | Fields it reads |
|---|---|---|
| `hold` | ease in to `rect`, hold, ease back | `rect` |
| `reveal` | start at `rect`, ease out to the full frame | `rect` |
| `drift` | move from `rect` to `rect2` across the block | `rect`, `rect2` |
| `impact` | cut straight to `rect`, then release | `rect` |
| `whip` | fast pan from `rect` to `rect2` | `rect`, `rect2` |

The preview applies this as a transform and the export builds a matching FFmpeg
graph from the same declaration, so what you approved is what renders. Only one
camera template can be active at a time.

### Sound

Reference the shared, loudness-matched library with a `_shared/` prefix, or ship
your own file in the pack folder:

```jsonc
"sfx": [
  { "file": "_shared/sfx/paper-drop.wav", "offset": 0.05, "gain": 0.5 },
  { "file": "_shared/sfx/tick.wav", "offset_ratio": 0.2, "gain": 0.3,
    "repeat_field": "items", "spread_ratio": 0.5 }
]
```

- `offset` is seconds from the block's start; `offset_ratio` is a fraction of its
  duration.
- `repeat_field` fires the sound once per item in a field. It counts a list, the
  lines of a text field, or a number — so it works for `strokes`, for a
  multi-line `items` field and for a `from` count without a hidden helper field.
- `spread_ratio` spaces the repeats across that fraction of the block;
  `repeat_spacing` gives a fixed gap instead.
- Every sound in `_shared/sfx/` is calibrated to one target, so `gain` is
  comparable across packs. 0.3–0.5 sits under speech; above 0.7 competes with it.

There is no background music in Editoro. Only foley, bound to a template.

---

## render.js

```js
import { paperCard, roundRectPath, fitText, drawLines, PALETTE }
  from "/tpl/_shared/kit.js";

const ID = "my-pack";

// Geometry once, used by both draw() and measure(), so the selection box on the
// preview is exactly the thing that was painted.
function layout(instance, A, viewport, ctx) {
  const u = A.viewportUnit(viewport);
  const variant = A.variant(ID);
  const scale = instance.scale * (variant.scale || 1);
  const fitted = fitText(ctx, instance.fields.text || "…", {
    maxWidth: viewport.width * (variant.max_width || 0.6) - 60 * u * scale,
    maxLines: variant.max_lines || 2,
    weight: 800, size: 62 * scale * u, minSize: 14 * u,
  });
  return { u, scale, fitted,
    width: fitted.width + 74 * u * scale,
    height: fitted.lines.length * fitted.size * 1.3 + 44 * u * scale };
}

registerTemplate(ID, {
  draw(ctx, instance, t, A) {
    const g = layout(instance, A, ctx.canvas, ctx);
    const card = A.motion(instance, t, "card");
    const text = A.motion(instance, t, "text");

    A.stage(ctx, card, () => {
      A.shadow(ctx, card.shadowSpec,
        c => roundRectPath(c, -g.width / 2, -g.height / 2, g.width, g.height, 10 * g.u));
      paperCard(ctx, A, { w: g.width, h: g.height, radius: 10, seed: instance.id });
    });
    A.stage(ctx, text, () => {
      drawLines(ctx, g.fitted.lines, {
        size: g.fitted.size, weight: 800, color: PALETTE.ink });
    });
  },

  measure(instance, A, viewport) {
    const probe = document.createElement("canvas").getContext("2d");
    probe.canvas.width = viewport.width; probe.canvas.height = viewport.height;
    const g = layout(instance, A, viewport, probe);
    return { width: g.width, height: g.height };
  },
});
```

The context arrives translated to the instance's position, so draw around
`(0, 0)`. `t` is normalised 0→1 across the block.

If your pack sets `"full_frame": true`, the context is **not** translated and you
draw in absolute canvas coordinates — remember to centre yourself with
`ctx.translate(W / 2, 0)` if your layout is symmetric.

### The runtime, `A`

| Call | Returns |
|---|---|
| `A.motion(instance, t, element?)` | The animation state: `opacity`, `scale`, `dx`, `dy`, `rotate`, `phase`, `k`, `velocity`, `seconds`. |
| `A.stage(ctx, state, drawFn)` | Applies that state — transform, opacity, motion blur — around your drawing. |
| `A.shadow(ctx, state.shadowSpec, pathFn)` | Layered drop shadow behind a silhouette. |
| `A.variant(id)` | The resolved layout for the current orientation. |
| `A.orientation()` | `"horizontal"` or `"vertical"`. |
| `A.unit(ctx)`, `A.viewportUnit(v)` | Pixels per design unit. Multiply every size by this. |
| `A.load(url)`, `A.video(url)` | Cached image / video elements. |
| `A.projectAsset(file)`, `A.instanceAssetUrl(instance)` | URLs for this project's media (PDF pages included). |
| `A.seeded(seed)` | A deterministic random function. **Never use `Math.random()`.** |
| `A.ease`, `A.spring(seconds, opts)` | The easing library and the spring solver. |
| `A.categoryColor(id)` | A pack's colour. |
| `A.fps()` | Timeline frame rate. |

### The kit, `/tpl/_shared/kit.js`

`paperCard` · `darkPanel` · `tapeStrip` · `grain` · `placeholder` ·
`roundRectPath` · `roundedImage` · `fitContain` · `fitCover` ·
`fitText` · `wrapLines` · `drawLines` · `chip` · `isRTL` · `applyDirection` ·
`highlighterSweep` · `scribbleEllipse` · `scribbleUnderline` · `handArrow` ·
`checkMark` · `formatNumber` · `splitItems` · `alpha` · `mix` · `clamp` ·
`PALETTE` · `art(name)`

### Rules

- **Deterministic only.** No `Math.random()`, no `Date.now()`. The export renders
  isolated frames out of order; anything stateful will disagree with the preview.
- **Everything scales by the unit.** A hardcoded pixel size looks right at 1080p
  and wrong everywhere else.
- **Text must fit.** Use `fitText`; it shrinks and ellipsises rather than
  overflowing the card. Someone will eventually type more than you planned for.
- **Implement `measure`.** Without it, selection and dragging do not match what
  is drawn.
- **Both orientations must be considered.** Read `A.variant()` rather than
  assuming a shape.
- **RTL.** `drawLines` handles direction automatically; if you place text
  yourself, call `applyDirection(ctx, text)` first.

---

## Checking your pack

```bash
.venv\Scripts\python.exe server.py --test        # validation and schema
.venv\Scripts\python.exe server.py --test-e2e    # renders and exports every pack
.venv\Scripts\python.exe tools/test_ui.py        # loads every render.js in a browser
```

Then look at it. Place the block, scrub through its entrance, and render a frame
through the MCP `render_frame` tool or the preview. Most of the defects that
survive the test suite are visual — a heading that overflows, a card in the
caption band, a full-frame pack drawn against the wrong origin — and all of them
are obvious in one still.
