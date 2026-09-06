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

In Claude Code, the bundled `.mcp.json` wires up a clone automatically; to edit
video from any directory, install it once at user scope instead:

```bash
claude mcp add -s user editoro -- \
  E:/Programming/Editoro/.venv/Scripts/python.exe E:/Programming/Editoro/mcp_server.py
```

For any other stdio client, add this to its MCP config:

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

The editor is started automatically if it is not already running, so none of
this requires `launch.cmd` to be open first.

The repo also ships a Claude skill in `.claude/skills/editoro` that teaches a
model the workflow above — which template earns which moment, why blocks are
anchored to quotes, and to look at a rendered frame before calling it done.

A whole edit looks like this from the model's side:

```
get_transcript          → read what is actually said
list_templates          → see what can go on screen
place_blocks            → place the entire edit in one call, anchored to quotes
set_look                → defocus the background and grade the picture
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

The **Look** panel (defocus and colour) is not a template — it applies to the
whole project. See [Look](#look).

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

### Sound you can switch off

Every block that makes a sound has a **Sound** control in the inspector, and
silencing one removes its foley from the mix *and* its markers from the SFX
track — so that track keeps being a truthful picture of what the export will
contain rather than a list of things that might play. Clicking a marker on the
SFX track selects the block that owns it and opens its panel, which is how you
find the one that keeps ticking.

---

## Breathing

No shot sits completely still. A very slow scale oscillation runs under the
whole project — about 2.4% over thirteen seconds at *Standard* — and every
overlay on screen is multiplied by the same factor at the same instant, so the
footage and the graphics move together instead of the graphics wobbling on top
of a steady frame.

It is in the **Look** panel, as four named strengths rather than a slider: the
tempo is shared by all of them, so a strength changes how deep the video
breathes and never how fast. Vertical breathes a little deeper than horizontal
for the same name, because the same percentage of a smaller frame reads as less
movement.

A hand-placed camera move switches it off for its own span, ramping over a third
of a second at each edge, so a punch-in and a pulse never stack. A `breathe`
block does the opposite: it overrides the strength over a stretch — including
asking for *off* through a section you want held still, or for *strong* through
one you want opened up.

The cost is honest and worth knowing: with breathing on, the footage is moving
everywhere, so there is no longer any span the export can stream-copy. An
untouched project with breathing off keeps the fast path exactly as it was.

---

## The stage

A full-frame animated backdrop for explainer stretches, in the same desk
language as everything else — drifting notebook ruling, paper grain, a slow ink
wash, or sparse torn-paper shapes. Its `framing` field decides what happens to
you:

- **behind** — the backdrop replaces the room and you stay live and full size,
  cut against the subject matte. Needs the Look to have been analysed once.
- **pip** — the backdrop takes the frame and you shrink into a window on it,
  cropped to the project's speaker region.
- **solo** — backdrop only, no camera.

It goes on the timeline as one long block, like everything else, and other
blocks layer on top of it normally.

---

## The thumbnail

**Thumbnail** in the toolbar. Grab a frame off the timeline or drop a picture
in, pick one of five arrangements, type the words, done. The arrangement is a
starting point rather than a cage — every element drags, scales, turns and
deletes, and you can add more.

Elements are a headline, a subhead chip, you cut out of the base frame and
enlarged, marker graphics, and a picture slot. You design once: each element
keeps a placement for 16:9 and one for 9:16, the same way timeline blocks do, so
switching orientation and switching back is lossless.

Saving writes a PNG at 1280×720 or 1080×1920 into `exports/`, ready to upload.
Separately, **First frame** holds the design on the very front of the exported
video for one frame — which is what a short opens on. That decision is also
offered in the export dialog, because it is usually made at the moment you
render rather than while designing. The cover is concatenated onto the finished
file after the frame audit has passed, so a deliberately added still can never
be mistaken for a lost one.

---

## Look

Two effects that apply to the whole project, both measured from the footage
rather than dialled in. Open **Look**, press *Analyse this footage* once, and
then there are exactly two sliders.

**Background defocus** is real depth, not a cutout. A depth model
([Depth Anything V3 Base](https://huggingface.co/onnx-community/depth-anything-v3-base),
about 400 MB, fetched on first use and cached) runs over the source once and
writes a depth matte beside it. On a GPU the network is converted to half
precision the first time it is used, which makes it run at roughly twice the
speed and half the memory — a depth map that ends up as an 8-bit matte cannot
tell the difference. The export then composites three depth slices — sharp,
softened, thrown away — so the wall behind the speaker falls off with distance
the way a fast lens does, instead of turning into a flat blurred card. The near
and far planes are fixed once from samples across the whole video, so the blur
cannot pump when the deepest thing in shot changes.

The blur itself is weighted by the matte before it is taken, not masked after.
Blurring the frame as it stands drags the subject's own bright edge out into
the wall behind them and traces a halo around their hair — the single most
recognisable sign of a fake defocus. Blurring colour-times-weight and weight
together and dividing one by the other at the end means the background is
blurred using only background.

Where there is an OpenCL GPU, the whole defocus — the weighting, both Gaussians
and the composite — runs as a kernel on it, with the grade folded into the same
pass. On 4K footage that is about three and a half times faster than the CPU
filter chain it replaces, and the blur is *better*: it is computed on a reduced
copy sized so the radius lands at a few pixels, which means the Gaussian is
sampled densely rather than stepped across in jumps. Sparse sampling of a wide
blur is what produces banding and ghosted edges. The CPU chain is still there
and is used when no GPU is available.

**Colour** is measured too. White balance comes from the neutrals with skin
excluded — average a talking-head frame and "grey" comes out skin-coloured, and
correcting toward that turns the speaker green. Exposure comes from the
histogram, contrast is added only where the picture is actually flat, and
saturation is pushed with a skin rolloff built into the table. The result is
written as a `.cube`, and the strength is baked into it: the preview uploads
that exact table as a 3D texture and the export hands the same file to
`lut3d` (or the same table as an image, when the grade rides along in the GPU
pass), so the two cannot disagree.

The slider is a curve rather than a straight mix, because the first part of a
colour correction carries nearly all of the visible change; half way along it
applies about two thirds of the measured correction. White balance rolls off
over the top of the range so that neutralising a warm room does not tint every
specular highlight cyan.

Everything the pass produces lives in `projects/<name>/look/`, and both halves
are invalidated automatically if the source footage is replaced.

## The preview copy

The editor does not play the master. A talking-head master is routinely 4K with
a two-second GOP and several gigabytes, and a browser asked to scrub that spends
its whole frame budget decoding — the render loop drops to around twenty hertz
and everything feels heavy. So the source is transcoded once into a small,
short-GOP copy that seeks instantly, and, when a look is on, a second copy with
the look already burned into it. Playing the baked copy costs one video decode
instead of two plus a shader chain per frame.

Both are built in the background, reported in the corner of the stage while they
run, and stored in `projects/<name>/preview/`. Neither is ever read by an
export, which always works from the master. Moving a look slider hands the
preview straight back to the live WebGL compositor so the picture responds
immediately, and the baked copy is rebuilt behind it — from the preview copy
rather than the master, with the blur radius scaled to match, which is the
difference between waiting under a minute and waiting a quarter of an hour.

The look is applied to the *source*, before any camera move, so a punch-in
magnifies an already-defocused frame the way a lens would. It also means an
export with a look on re-encodes every span — there is no untouched footage
left to copy.

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
  look/              depth matte and the measured .cube, if a look was built
  preview/           the small proxy the editor plays; never read by an export
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
