# Editoro

Editoro is a complete, local template-driven video editor for consistent talking-head videos. It runs on Windows, keeps every project on your computer, and is usable from the laptop or another device on the same Wi-Fi network.

## Start

1. Install Python 3.11 or newer and FFmpeg if they are not already installed.
2. Double-click `launch.cmd`.
3. Leave the command window open while editing.

The first launch creates a private `.venv`, installs the exact dependencies, and downloads Editoro's private browser renderer. Later launches reuse everything and start quickly. If setup or startup fails, the command window stays open and shows the reason.

Editoro opens at `http://127.0.0.1:8765`. The command window also prints the local-network address for an iPad or phone.

## Workflow

1. Create a project.
2. Import the main source video.
3. Generate editable captions locally with Whisper, or import an `.srt`/Whisper `.json` transcript.
4. Place templates directly, or paste timestamped directives in the Directives panel.
5. Move and scale visuals on the preview; move, trim, split, and delete blocks on the timeline.
6. Export with **LOSSLESS (smart)** for the recommended result or **High quality** for a full re-encode.

Projects, uploaded media, captions, directives, and exports live in `projects/<project-name>/`.
Deleted projects are moved to a timestamped, recoverable folder instead of being erased immediately.

## Included template packs

- Keyword notebook card
- Image pop/polaroid
- Inserted video clip
- Document/PDF highlighter
- Speaker punch-in
- Speaker zoom-out
- Framed screen/article capture
- Ambient paper motion layer
- Arabic/Latin captions

Each pack owns its design specification, generated visual assets, fonts where needed, and organic sound effects inside `templates/<pack>/`.

## Controls

- Space: play/pause
- Left/Right: move exactly one frame; hold Shift for ten frames
- S: split the selected source segment or visual at the playhead
- Home/End: jump to the beginning/end
- + / -: zoom the timeline; 0: fit the whole edit
- Ctrl+Z: undo
- Ctrl+Shift+Z or Ctrl+Y: redo
- Delete or Backspace: remove the selected visual or caption
- Timeline ruler drag: scrub
- Timeline block drag/edges: move or trim
- Ctrl+mouse-wheel or pinch: zoom around the pointer, from full-project scale down to individual frames
- Mouse-wheel: scroll horizontally; Alt+wheel: scroll tracks vertically
- Preview drag, corner handle, or pinch: position and scale a visual
- Speaker button and volume slider: control all monitoring audio without changing export levels
- Fullscreen button: open the complete preview and docked transport without covering footage
- Download button: save the newest export directly to the current desktop or phone
- Double-click a split source segment: delete it using the visible Ripple setting

Timeline edits snap to the source frame rate. The ruler automatically changes from seconds to frame timecode at deep zoom, and the Snap toggle adds magnetic alignment to nearby block edges and the playhead.

## Local transcription

The **Generate captions** dialog runs faster-whisper entirely on this computer. `large-v3` is the accuracy-first default; `large-v3-turbo` is the faster option. Editoro uses the NVIDIA GPU when available and falls back to CPU automatically. English, Arabic, auto language detection, word timestamps, silence filtering, custom spelling terms, progress, and cancellation are built in. New captions replace the caption track only after a successful run, so a failed or canceled job cannot destroy existing edits.

The first use downloads the selected model into `.models/whisper/`. Generated word-level data is also saved beside the project as `transcript.generated.json`; caption text, timing, splits, and trims remain directly editable in the normal timeline.

## Export

- **LOSSLESS (smart)** preserves clean source spans where possible and renders only edited regions.
- **High quality** performs a consistent full encode and can output at source, 1080p, or 720p resolution.
- Edited overlay frames stream directly from the private renderer into FFmpeg; no per-frame PNG files are written.
- NVENC uses the local NVIDIA GPU when available, with a fast high-quality CPU fallback.
- Original speech is rebuilt from source cuts once, then template sound effects and inserted-clip audio are mixed without per-segment AAC drift.
- Progress, safe cancellation, completed-file history, one-tap latest download, CPU/GPU fallback, and the detailed `export.log` are available from the export dialog/project folder.

## Built-in checks

Run all local backend, template, parser, API, range-streaming, and safety checks with:

```text
.venv\Scripts\python.exe server.py --test
```

Run the complete synthetic edit/export regression—including two source cuts, synchronized audio, all nine template packs, HQ output, and smart-lossless output—with:

```text
.venv\Scripts\python.exe server.py --test-e2e
```

FFmpeg, FFprobe, all nine template renderers, CPU/GPU encoding availability, and any invalid pack are also reported on the home screen and at `/api/diagnostics`.
