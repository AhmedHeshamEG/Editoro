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
3. Import an `.srt` caption file or Whisper `.json` transcript.
4. Place templates directly, or paste timestamped directives in the Directives panel.
5. Move and scale visuals on the preview; move, trim, split, and delete blocks on the timeline.
6. Export with **LOSSLESS (smart)** for the recommended result or **High quality** for a full re-encode.

Projects, uploaded media, captions, directives, and exports live in `projects/<project-name>/`.

## Included template packs

- Keyword notebook card
- Image pop/polaroid
- Inserted video clip
- Document/PDF highlighter
- Speaker punch-in
- Framed screen/article capture
- Ambient paper motion layer
- Arabic/Latin captions

Each pack owns its design specification, generated visual assets, fonts where needed, and organic sound effects inside `templates/<pack>/`.

## Controls

- Space: play/pause
- Ctrl+Z: undo
- Ctrl+Shift+Z: redo
- Delete or Backspace: remove the selected visual or caption
- Timeline ruler drag: scrub
- Timeline block drag/edges: move or trim
- Ctrl+mouse-wheel or pinch: zoom timeline
- Preview drag, corner handle, or pinch: position and scale a visual
- Double-click a split source segment: delete it with ripple

## Built-in checks

Run all local backend, template, parser, API, range-streaming, and safety checks with:

```text
.venv\Scripts\python.exe server.py --test
```

FFmpeg, FFprobe, the eight template packs, and the renderer are also reported at `/api/diagnostics`.
