---
name: editoro
description: Edit a talking-head video end to end with the Editoro MCP server - read the transcript, cut the footage, place template graphics anchored to what was said, check the result visually, and export. Use whenever the user wants a video edited, graphics placed on footage, captions generated or styled, b-roll or screenshots inserted, or an Editoro project exported.
---

# Editing with Editoro

Editoro is a local, template-driven video editor. Every on-screen element comes
from a template with a fixed look, tuned motion and its own foley, so the work is
choosing **what** goes on screen and **when** — never how it should look.

The MCP server exposes the whole editor. If its tools are not available, tell the
user to add it to their MCP config (`python mcp_server.py --print-config` prints
a ready block) and stop there rather than trying to edit files directly.

## Before touching anything

1. `diagnostics` — confirm FFmpeg is present and that no template pack failed to
   load. A failed pack silently removes an option you might reach for.
2. `get_project` — source, length, orientation, what is already placed.
3. `get_transcript` — **read the whole thing** before placing anything.
4. `list_templates` — once per session. The field descriptions are the contract.

If there is no transcript, run `generate_captions` first. Everything downstream
depends on knowing what is said and when.

## Deciding what goes on screen

Read the transcript looking for the moments that *earn* a graphic:

| The speaker… | Template |
|---|---|
| says a number or a measurement | `stat-pop` |
| names a term for the first time | `definition` |
| quotes someone | `quote-card` |
| lists several things | `checklist` or `steps` |
| weighs two things against each other | `compare` |
| cites a study, article or dataset | `citation` |
| refers to a document, article or paper | `highlight` |
| refers to a tweet, post or message | `chat-bubble` |
| refers to a screenshot or interface | `screen` |
| refers to code | `code-card` |
| lands a key phrase worth pinning | `keyword` |
| changes subject entirely | `chapter-card` |
| makes an aside | `sticky-note` |
| points at something on screen | `arrow-point`, `circle-emphasis`, `spotlight` |

Not every sentence needs one. A graphic every few seconds is decoration.
Stretches of plain speaking are fine and make the graphics that do appear land
harder.

## Placing

Send the **entire edit in one `place_blocks` call**, not one call per block.

Anchor with `quote`, not `at`. A quote still points at the right moment after
something is cut out; a timestamp does not:

```json
{"template": "stat-pop", "quote": "about eighty seven percent",
 "duration": 2.5,
 "fields": {"value": "87", "suffix": "%", "label": "of the file"}}
```

Rules that keep a video looking like one video:

- **Keyword and quote text must be verbatim.** These templates pin words the
  speaker actually said. Paraphrasing is the fastest way to look automated.
- **One camera move at a time.** `place_blocks` will refuse a second overlapping
  one; do not work around it by retiming until they fit.
- **Leave placement alone** unless a rendered frame shows a problem. Every
  template already has a tuned 16:9 and 9:16 layout.
- **Do not clear a meme block's review flag.** Comedy is reviewed by a human.
- Cut before you place. Cutting afterwards means re-checking everything.

## Checking your own work

After placing, `render_frame` at three or four moments — including one where two
blocks are close together, and one near the end. Look at the images.

Things that only show up visually: a heading that ran long, a card sitting over
the speaker's face, two graphics overlapping, a caption colliding with a lower
graphic. Fix them with `update_blocks`.

Do not skip this. A coordinate list always looks fine.

## Finishing

- `set_caption_style` if captions collide with anything you placed.
- `export` with mode `lossless` unless the user asked for a different resolution.
- Tell the user where the file is and what you placed, briefly.

## When to ask instead of guessing

- A quote could plausibly refer to two different things.
- A claim seems to need a source card but no source was named.
- The user's brief implies a graphic you have no asset for — place the block
  anyway (it is flagged as awaiting an asset) and say which files are needed.
- The footage needs cutting in a way that changes meaning.

## Reference

- Tool reference: `docs/MCP.md`
- Template authoring: `docs/TEMPLATES.md`
