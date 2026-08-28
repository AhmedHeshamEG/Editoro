# Editoro

A local, template-driven video editor for talking-head videos — and an MCP server
that lets a model do the editing.

Everything runs on your own machine. The footage never leaves it, the
transcription is local, the rendering is local, and the agent that places the
graphics talks to `127.0.0.1`.

```
┌──────────────┐        ┌──────────────┐        ┌──────────────┐
│  browser UI  │◄──────►│  server.py   │◄──────►│ mcp_server.py│
│  (index.html)│   ws   │ FastAPI +    │  http  │  MCP tools   │
└──────────────┘        │ FFmpeg       │        └──────────────┘
                        └──────┬───────┘                ▲
                               │                        │
                     templates/<pack>/            any MCP client
```

Both the UI and the agent edit the same project state, live: a block placed by a
model appears in an open browser tab immediately, and a card you drag by hand is
what the next tool call sees.

---

## Why it exists

Editing a consistent explainer channel is mostly the same five decisions made a
hundred times: which graphic, where, how big, how long, with what sound. Editoro
removes those decisions by making every on-screen element a **template** with a
fixed look, a tuned animation, its own foley, and a layout for both 16:9 and
9:16. You choose *what* and *when*. Everything else is already decided.

That same property is what makes it drivable by a model. There is no free-form
canvas to reason about — just a catalogue of templates with typed fields.

---

## Getting started

You need **Python 3.11+** and **FFmpeg** (with `ffprobe`) on your PATH.

1. Double-click **`launch.cmd`**.
2. Leave the window open while you edit.

The first launch creates a private `.venv`, installs the pinned dependencies and
downloads the headless browser used for rendering. Later launches reuse all of
it. If anything fails, the window stays open with the reason.

Editoro opens at `http://127.0.0.1:8765`, and the window also prints a LAN
address so you can edit from an iPad or phone on the same Wi-Fi.

---

## Editing by hand

1. Create a project and import the source video.
2. Generate captions locally with Whisper, or import an `.srt` / Whisper `.json`.
3. Click a template to place it, or paste a timestamped directive list.
4. Drag on the preview to move and scale; drag on the timeline to retime.
5. Export.

| Key | Action |
|---|---|
| Space | Play / pause |
| ← → | One frame; hold Shift for ten |
| S | Split the selected block or source segment |
| Home / End | Jump to start / end |
| + − 0 | Zoom the timeline in, out, to fit |
| Ctrl+Z / Ctrl+Shift+Z | Undo / redo |
| Delete | Remove the selection |
| F | Fullscreen preview |

Ctrl+wheel zooms around the pointer, from whole-project scale down to individual
frames. The ruler switches from seconds to frame timecode as you go deeper.

---

## Editing with a model

`mcp_server.py` exposes the whole editor over the Model Context Protocol. It is
a plain, spec-compliant MCP server — Claude Desktop, Claude Code, and any other
MCP client all speak to it the same way.

```bash
python mcp_server.py                                  # stdio
python mcp_server.py --transport streamable-http --port 8766
python mcp_server.py --print-config                   # paste-ready client config
```

For a stdio client, add this to its MCP config:

```json
{
  "mcpServers": {
    "editoro": {
      "command": "E:/Programming/Editoro/.venv/Scripts/python.exe",
      "args": ["E:/Programming/Editoro/mcp_server.py"]
    }
  }
}
```

The editor is started automatically if it is not already running.

A whole edit looks like this from the model's side:

```
get_transcript          → read what is actually said
list_templates          → see what can go on screen
place_blocks            → place the entire edit in one call, anchored to quotes
render_frame            → look at the result and fix what is wrong
export                  → write the MP4
```

Blocks are anchored to **spoken words**, not timestamps:

```json
{"template": "stat-pop", "quote": "about eighty seven percent",
 "fields": {"value": "87", "suffix": "%", "label": "of the file"}}
```

`render_frame` returns a real composited frame — footage, camera move, templates
and captions — so the model can check its own work instead of reasoning about
coordinates blind.

Full tool reference: **[docs/MCP.md](docs/MCP.md)**.

---

## Templates

Thirty-seven packs ship with Editoro. Each is a folder under `templates/`, and
each carries its own look, motion, foley and both orientation layouts.

| Category | Packs |
|---|---|
| **Text** | keyword · stat-pop · quote-card · definition · checklist · compare · steps · progress-bar · sticky-note · equation · citation |
| **Annotation** | highlight · arrow-point · circle-emphasis · underline-emphasis · spotlight · censor |
| **Image / video** | image-pop · video-clip · meme-frame · split-screen · pip-speaker |
| **Screen** | screen · chat-bubble · code-card |
| **Camera** | punch-in · zoom-out · ken-burns · punch-cut · whip-pan · frame-stamp |
| **Structure** | chapter-card · hook-card · end-card · countdown |
| **Always on** | captions · ambient |

**Adding a template never requires touching `server.py` or `index.html`.** Drop a
folder with a `template.json` and a `render.js` into `templates/` and restart.
See **[docs/TEMPLATES.md](docs/TEMPLATES.md)**.

### Motion

Animation is declared, not coded. A pack describes what it wants and the shared
engine does it identically in the preview and the export:

```json
"motion": {
  "in":  {"type": "spring", "stiffness": 220, "damping": 17, "ms": 620,
          "from": {"opacity": 0, "scale": 0.58, "y": -78, "rotate": -7}},
  "out": {"type": "ease", "easing": "inCubic", "ms": 240},
  "stagger": {"step_ms": 62, "elements": ["card", "text"]},
  "idle":   {"amplitude": 0.0028, "hz": 0.13},
  "blur":   {"motion": true, "max": 7},
  "shadow": {"layers": 3, "elevation": 26, "opacity": 0.46, "lift": true}
}
```

The springs are solved analytically rather than integrated, so the value at a
given moment is the same whether you scrub, play, or render one isolated export
frame. Directional motion blur is sampled from real velocity, shadows are
layered, and cards keep a faint idle drift so nothing sits perfectly dead.

### Two layouts, always

Every template ships a 16:9 and a 9:16 variant. The active one follows the
footage automatically; you can pin it with the layout button (or
`set_orientation`) when you are cutting vertical from horizontal footage. Each
orientation remembers its own placement, so switching back and forth is
lossless.

### Sound

All foley lives in `templates/_shared/sfx/`, generated and calibrated by
`tools/gen_sfx.py`. Every sound is measured with BS.1770 K-weighting and
normalised to one shared target (or to a true-peak ceiling, for transients that
physically cannot reach it), which is what makes a pack's `gain: 0.5` mean the
same thing everywhere. There is no background music anywhere in Editoro — only
foley bound to a template.

Textures are generated too, by `tools/gen_art.py`: seamless procedural paper,
grain, tape and marker strokes, written as PNGs with no imaging dependency.

---

## Captions

Local `faster-whisper` with word-level timings, GPU when available and CPU
otherwise. `large-v3` is the accuracy default; `large-v3-turbo` is faster.
English, Arabic and auto-detection are all supported, and new captions replace
the old ones only after a run succeeds.

Placement is per orientation and has two levels: a project-wide position that
everything follows, and a per-caption override for the one line that needs to
move. Drag a caption on the preview and it pins itself there; the inspector
sends it back. Captions can group into short chunks or hold a whole sentence,
and the word being spoken can lift as it is said.

---

## Export

- **LOSSLESS (smart)** stream-copies every span you did not touch and renders
  only the edited ones.
- **High quality** re-encodes everything consistently, optionally at 1080p or
  720p.

The parts that make this reliable:

- Spans are joined through **MPEG-TS**, because a stream-copied span and a
  re-encoded span carry different parameter sets — MP4 concatenation produces a
  file that plays correctly only until the first join.
- Every span's frame count is planned from **cumulative timeline positions**, so
  rounding cannot accumulate into drift against the audio.
- The re-encoder **matches the source** pixel format and profile.
- Short clean gaps between graphics are absorbed into the neighbouring rendered
  span rather than becoming their own process.
- Overlay frames stream straight from the headless renderer into FFmpeg. No
  per-frame PNG files are written.
- Speech is rebuilt once from the source cuts, foley is placed sample-accurately,
  and the mix is brickwall-limited before encoding.
- The result is verified by frame count, and a mismatch writes a per-span audit
  into `export.log` naming the span that disagreed.

NVENC is used when the GPU supports it, with an automatic CPU fallback.

---

## Layout

```
server.py            all backend logic — API, transcription, export, MCP support
index.html           the entire frontend: motion engine, preview, timeline, panel
mcp_server.py        the MCP server
launch.cmd           one-click setup and start
templates/
  _shared/           kit.js, generated art/ and sfx/ used by every pack
  <pack>/            template.json + render.js (+ its own assets)
tools/
  gen_sfx.py         synthesise and calibrate the foley library
  gen_art.py         generate the procedural textures
  test_mcp.py        end-to-end check of the agent surface
  test_ui.py         headless smoke test of the browser UI
projects/<name>/     source video, assets, transcript, exports, project.json
docs/                MCP and template-authoring references
```

Projects are plain folders. Deleting one moves it to a timestamped, recoverable
folder rather than erasing it.

---

## Tests

```bash
.venv\Scripts\python.exe server.py --test        # models, parser, API, safety
.venv\Scripts\python.exe server.py --test-e2e    # every pack, both export modes
.venv\Scripts\python.exe tools/test_mcp.py       # the whole agent workflow
.venv\Scripts\python.exe tools/test_ui.py        # the real page in a real browser
```

`--test-e2e` places one instance of **every installed template**, exports it
twice and checks the joins frame by frame. A renderer that throws, a missing
shared asset or a camera mode without a filter graph fails there rather than in
somebody's export.

---

## License

MIT — see [LICENSE](LICENSE).

The five original foley recordings come from [Mixkit](https://mixkit.co/license/#sfxFree),
which permits personal and commercial use without attribution; see
[templates/SFX_SOURCES.md](templates/SFX_SOURCES.md). Everything else — code,
textures, synthesised sounds — is original and covered by the MIT license above.
Inter and Rubik are licensed under the SIL Open Font License.
