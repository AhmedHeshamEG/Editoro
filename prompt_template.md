# Build Prompt — New Editoro Template Pack: <TEMPLATE NAME>

You are adding a **new template pack** to Editoro, an existing template-driven video editor. You must NOT modify `server.py` or `index.html` — packs are pure drop-in folders. Read the attached `context_template.md` for this template; it is the source of truth for look, fields, animation, SFX, and rules.

## Deliverables (all inside `templates/<template-id>/`)
1. `template.json` — machine spec derived from the context file:
   - `id`, `display_name`, `category_color`
   - `directive_verb` (or null)
   - `fields`: typed list (`text` / `asset:image` / `asset:video` / `strokes` / `rect` / `none`)
   - `zones`: default position + scale for `horizontal` and `vertical`
   - `animation`: named entrance/hold/exit with durations (ms) and easing
   - `sfx`: file references with trigger points and gains
   - `default_duration` (seconds)
2. `render.js` — self-registering ES module using the Editoro overlay-engine API:
   - `registerTemplate('<template-id>', { draw(ctx, state, t, assets), measure(state) })`
   - `t` is normalized time within the instance (0→1) at the project fps; the same module is executed identically by the live preview and the headless export renderer — **no DOM-only or preview-only tricks**, deterministic per frame, no `Date.now()`/randomness (seeded randomness only, seed from instance id).
   - Respect the pack's `category_color` only for UI, never inside the rendered video unless the design says so.
3. Asset files (PNG/SVG textures, frames) and SFX files (wav) referenced by `template.json`.
4. `context_template.md` — the filled context file, kept in the folder.

## Constraints
- **Aesthetic:** student-desk skin — notebook paper, sticky notes, highlighter marker; tactile, warm, organic motion (overshoot/settle easings), never corporate, never distracting from the speaker.
- **Fonts:** use only the fonts already shipped in Editoro packs (UI: Inter; on-video text: Rubik). No new fonts.
- **SFX:** organic sounds only, short, synced to the animation triggers defined in `template.json`. No music.
- **Performance:** draw() must run comfortably at 60fps in preview on an iPad; pre-load and cache assets via the provided `assets` handle.
- **Determinism:** identical output for identical (state, t). This is what guarantees preview == export.
- Honor every rule in the context file's "Rules & taste constraints" section literally.

## Acceptance
- Dropping the folder into `templates/` and restarting Editoro makes the template appear in the panel with its color, placeable, fillable, animated with SFX in preview, and rendered identically in an export.
- No edits anywhere outside the pack folder.
