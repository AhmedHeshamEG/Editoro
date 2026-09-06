# Editoro MCP tools

Every tool takes `project` (the project name) unless noted. Times are **timeline
seconds** except in `set_cuts` and `remove_range`, which are source seconds.
Positions are a 0..1 fraction of the frame.

Call `list_templates` for the field contract of any template — this file
deliberately does not copy it, because the packs change and the tool does not.

## Orientation

| Tool | What it does |
|---|---|
| `diagnostics()` | Editoro, FFmpeg, GPU encoder and every template pack healthy? Run first. |
| `list_projects()` | Every project, with source footage, length and orientation. |
| `create_project(name)` | Create an empty project and open it. |
| `get_project(project, include_timeline)` | Source, length, orientation, what is already placed. |
| `list_templates(category, detailed)` | The catalogue, with fields and what each is for. `detailed` adds layouts, colours and sounds. |
| `set_notes(project, notes)` | Leave an editing plan or open questions on the project. Survives the session. |

## Reading the footage

| Tool | What it does |
|---|---|
| `get_transcript(project, format)` | What is said, with timings. `format` gives blocks or SRT. Read the whole thing before placing. |
| `find_quote(project, quote, limit)` | When something was said, in timeline seconds. Tolerates punctuation, case and block boundaries. |
| `generate_captions(project, model, language, terms, wait)` | Local Whisper with word-level timings. `terms` seeds names and jargon it would otherwise mangle. |
| `caption_status(project)` | Progress of a running transcription. |

## Cutting

Do this **before** placing. Cutting afterwards moves every block you positioned.

| Tool | What it does |
|---|---|
| `set_cuts(project, keep, ripple)` | Cut the source down to the spans worth keeping. Source seconds. |
| `remove_range(project, start, end)` | Drop one span — a stumble, a pause, a retake. Source seconds. |
| `import_source(project, path)` | Set the source footage. Resets cuts to the whole video. |

## Placing

| Tool | What it does |
|---|---|
| `place_blocks(project, blocks, replace_existing)` | The main editing tool. Send the whole edit in one call. Anchor each block with `quote`, not `at`. |
| `update_blocks(project, updates)` | Retime, restyle, fill in or delete blocks already placed. |
| `clear_timeline(project, templates)` | Remove placed blocks. Cuts, captions and media are untouched. |
| `apply_directives(project, text)` | Parse a written shot list and populate the timeline in one action. Unparseable lines come back for review, never dropped. |
| `set_orientation(project, mode)` | Which of each template's two layouts is used. `auto` follows the footage. Switching is lossless — each orientation's placement is remembered. |

## The look

| Tool | What it does |
|---|---|
| `set_look(project, defocus, grade, analyze, wait)` | Background defocus and automatic colour for the whole project. `analyze=True` reads the footage once and builds what both need; after that the two amounts are instant. Nothing else is adjustable — the rest was measured. |

## Assets

| Tool | What it does |
|---|---|
| `add_asset(project, path)` | Copy a local file into the project so a template can use it. |
| `list_assets(project)` | What media the project already has. |
| `get_asset_info(project, asset)` | Real pixel dimensions, aspect ratio, page count. |
| `find_text_in_asset(project, asset, query, page)` | Locate a phrase on a page and get highlight coordinates for it. |
| `highlight_lines(project, asset, quote, highlight, page, duration)` | Place a highlighted document page in one call. Use this rather than guessing coordinates. |

## Captions

| Tool | What it does |
|---|---|
| `set_caption_style(project, position, grouping, words_per_chunk, font_size, highlight_active_word, backing_box, regroup)` | Look and placement, **per orientation**. |
| `edit_captions(project, edits)` | Fix mistranscriptions, retime, delete, or `pin_to` one block away from the global placement. A pin applies to the current orientation only. |

Rewriting a block's text drops its word timings, so per-word highlighting stops
for that block. The old timings described the old words.

## Finishing

| Tool | What it does |
|---|---|
| `render_frame(project, at, width)` | One finished frame — footage, camera move, templates, captions. **Look at it.** |
| `export(project, mode, resolution, wait)` | Render to MP4 in the project's exports folder. `lossless` unless asked otherwise. |
| `export_status(project)` | Completed exports, newest first, plus any recent failure. |
