# Contributing

Editoro is deliberately small: three files of application code, a folder of
templates, and a folder of tools that generate its assets.

```
server.py      backend — API, project state, transcription, export, MCP support
index.html     frontend — motion engine, preview, timeline, panel. One file.
mcp_server.py  the MCP server
```

If a change makes one of those files meaningfully harder to read, it is probably
the wrong change.

## Setting up

Run `launch.cmd` once. It creates `.venv`, installs the pinned dependencies and
downloads the headless Chromium used for rendering. After that:

```bash
.venv\Scripts\python.exe server.py --no-browser     # run it
.venv\Scripts\python.exe server.py --test           # models, parser, API, safety
.venv\Scripts\python.exe server.py --test-e2e       # every pack, both export modes
.venv\Scripts\python.exe tools/test_mcp.py          # the agent workflow
.venv\Scripts\python.exe tools/test_ui.py           # the real page in a browser
```

All four must pass before a change lands. `--test-e2e` and `test_ui.py` need
FFmpeg and the renderer; they are the two that catch the failures that matter.

## The rule that shapes everything

**Adding a template must never require editing `server.py` or `index.html`.**

A template is a folder with a `template.json` and a `render.js`. Colours, motion,
foley, both orientation layouts, the fields the editor draws and the schema an
agent reads all come from that folder. If a new template needs engine support,
add the capability to the engine generically — a `camera.mode`, a field type, a
motion property — and never a branch on a template id.

See `docs/TEMPLATES.md`.

## Things that are easy to get wrong

**Determinism.** The preview scrubs; the exporter renders isolated frames out of
order. Anything stateful — `Math.random()`, `Date.now()`, an integrated spring —
makes the two disagree, and the disagreement only shows up in a finished export.
Use `A.seeded()` and the analytic spring solver.

**Frame counts, not seconds.** Export spans are planned from cumulative timeline
positions so rounding cannot accumulate. Never compute a span's length in
isolation; `plan_segment_frames` exists for this.

**Joins.** Spans go through MPEG-TS because a stream-copied span and a
re-encoded span carry different parameter sets. Do not "simplify" this back to
concatenating MP4s — it produces a file that plays correctly until the first
join and then does not.

**Units.** Everything visual scales by `A.unit(ctx)`. A hardcoded pixel size is
correct at exactly one resolution.

**Both orientations.** Every template ships 16:9 and 9:16. The scanner rejects a
pack that only defines one, on purpose.

## Regenerating assets

Textures and foley are generated, seeded and committed:

```bash
.venv\Scripts\python.exe tools/gen_art.py     # templates/_shared/art/*.png
.venv\Scripts\python.exe tools/gen_sfx.py     # templates/_shared/sfx/*.wav
```

`gen_sfx.py` prints the measured loudness of every sound and which limit bound
it. A sound that hits neither the loudness target nor the true-peak ceiling is a
failure, not a warning — it means the library is no longer calibrated and pack
`gain` values have stopped being comparable.

## Style

Match what is there. The Python is typed and uses full words; the JavaScript in
`index.html` is dense by design because it is one file; the template renderers
are meant to read as a description of one visual rather than canvas boilerplate.

Comment the *why*, especially where the code looks unnecessarily careful — those
are the places where the obvious version was tried first and was wrong.
