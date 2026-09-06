---
name: editoro
description: Edit a talking-head video end to end with the Editoro MCP server - read the transcript, cut the footage, place template graphics anchored to what was said, check the result visually, and export. Use whenever the user wants a video edited, graphics placed on footage, captions generated or styled, b-roll or screenshots inserted, or an Editoro project exported.
---

# Editing with Editoro

Editoro is a local, template-driven video editor. Every on-screen element comes
from a template with a fixed look, tuned motion and its own foley, so the work is
choosing **what** goes on screen and **when** — never how it should look.

The MCP server exposes the whole editor and starts it on the first tool call, so
nothing needs to be running first. If the `editoro` tools are not available at
all, the server is not installed: tell the user to run
`claude mcp add -s user editoro -- <editoro>/.venv/Scripts/python.exe <editoro>/mcp_server.py`
and restart, and stop there rather than editing project files directly.

The full tool list is in `reference/tools.md`. Read it when you need a tool you
have not used yet.

## Two conventions that decide most mistakes

**Time is timeline seconds** — position in the finished edit, not in the raw
file. Cuts apply first, so a block at 12.0 s lands twelve seconds into the
export. The only exceptions are `set_cuts` and `remove_range`, which necessarily
talk about the source file and say so.

**Position is a 0..1 fraction of the frame**, identical at any resolution and in
both orientations. You rarely set it at all.

## Before touching anything

1. `diagnostics` — confirms FFmpeg is present and that no template pack failed to
   load. A failed pack silently removes an option you might reach for.
2. `get_project` — source, length, orientation, what is already placed.
3. `get_transcript` — **read the whole thing** before placing anything.
4. `list_templates` — once per session. The field descriptions are the contract.

If there is no transcript, run `generate_captions` first (it is local Whisper and
takes a while; `caption_status` polls it). Everything downstream depends on
knowing what is said and when.

## Order of operations

Cut, then place, then style captions, then check, then export. Cutting after
placing moves every block you already positioned and forces you to re-check all
of it.

## Deciding what goes on screen

Read the transcript looking for the moments that *earn* a graphic:

| The speaker… | Template |
|---|---|
| says a number or a measurement | `stat-pop` |
| gives a proportion or a share | `progress-bar` |
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

### How long a block should be

Every template ships a `default_duration` that is right most of the time. Omit
`duration` and you get it. Override it when the speech gives you a reason to,
not by habit.

The number that actually matters is not the duration but **what is left after
the animation**. Every block spends roughly 0.5-0.8 s arriving and 0.25-0.4 s
leaving, and those are not reading time. A 1.5 s card is on screen for about
half a second of stillness, which is not long enough to read anything.

| Block | Typical | Floor, and why |
|---|---|---|
| `keyword` | 2.0-2.5 s | 1.2 s. Two or three words, read at a glance. |
| `stat-pop`, `progress-bar` | 2.5-3.5 s | **2.0 s.** The value counts up over about a second *after* the card lands. Below 2 s the number is still climbing when it leaves. |
| `definition`, `citation`, `sticky-note` | 3.5-4.5 s | 2.5 s. A phrase plus a line of detail. |
| `quote-card`, `compare`, `checklist` | 4.5-6 s | 3.5 s, and add ~1 s per item past three. |
| `steps`, `code-card` | 5.5-7 s | Stagger means the last item lands late; give it room to be seen after it does. |
| `chapter-card`, `hook-card` | 3-3.5 s | These are punctuation. Do not let them linger. |
| `screen`, `image-pop`, `meme-frame` | 3-4.5 s | Long enough to look at the picture, not long enough to study it. |
| `video-clip`, `pip-speaker`, `split-screen` | 4-8 s | Match the clip. A clip cut short mid-motion reads as a mistake. |
| `punch-in`, `zoom-out`, `ken-burns` | 3-8 s | Camera moves are slow by nature; under ~2.5 s they read as a bump. |
| `whip-pan`, `punch-cut` | 0.7-1.2 s | Transitions. Never stretch them. |

Two rules that override the table:

- **Never outlast the sentence.** A card still up when the speaker has moved on
  is the single most common way an edit looks automated. End it on or just
  before the end of the thought it belongs to.
- **A block may not extend past the end of the video.** Check `duration` in
  `get_project` before placing anything near the end.

### When to put it on screen

Anchor with `quote` and Editoro starts the block at the moment those words are
spoken. That is right for `keyword`, `stat-pop` and anything that pins what was
just said — the graphic and the word land together.

It is wrong for anything the speaker is about to *explain*. A definition, a
chart, a screenshot or a comparison should already be on screen when they start
talking about it, so use the quote of the phrase that introduces it rather than
the payload itself:

```json
{"template": "definition", "quote": "there is a term for this",
 "duration": 4.0, "fields": {"term": "…", "meaning": "…"}}
```

Spacing, which matters as much as placement:

- **Leave about a second of clear frame between blocks.** Back-to-back graphics
  read as a slideshow — one block's exit should finish before the next arrives.
- **Do not place a block inside another block's entrance.** Two things
  animating at once is where an edit starts to look busy rather than designed.
- **Let the opening breathe.** Nothing in the first ~1.5 s except a `hook-card`
  if the video has one.
- **Silence is a choice.** Ten seconds of plain speaking between graphics is
  normal and makes the next one land harder.

### Where it goes

**Leave `x`, `y` and `scale` alone.** Every template already has a tuned 16:9
and 9:16 layout, and those layouts are the reason the templates look designed.
Setting placement by hand is how a project stops looking like one video.

What you do need to know is what already occupies the frame, because that is
what you are avoiding:

- **The speaker's face** sits in the upper-middle of a vertical frame. The
  templates that put themselves high (`keyword` at y≈0.19, `stat-pop` at
  y≈0.26) are sized to clear it. This is also why `depth: "behind"` exists.
- **Captions** sit low — y≈0.78 vertical, y≈0.86 horizontal. `citation`
  (y≈0.9) and `progress-bar` (y≈0.7) are the two that get close to them.
  Check a rendered frame if you place either while captions are on.
- **Full-frame templates** (`pip-speaker`, `split-screen`, `chapter-card`,
  `end-card`, `backdrop-blur`, every camera move) cover the whole picture.
  Nothing else belongs on screen underneath one except captions.
- **Two blocks in the same region at the same time** are reported back to you
  in `overlapping`. Different tracks stack them, they do not separate them.

Move a block only when a rendered frame shows a specific problem, and move it
the smallest distance that fixes it.

Rules that keep a video looking like one video:

- **Keyword and quote text must be verbatim.** These templates pin words the
  speaker actually said. Paraphrasing is the fastest way to look automated.
- **Give counting templates room.** `stat-pop` and `progress-bar` animate their
  value over about a second, deliberately outlasting the card's entrance. Under
  ~2 s the number is still climbing when the block leaves.
- **One camera move at a time.** `place_blocks` will refuse a second overlapping
  one; do not work around it by retiming until they fit.
- **Leave placement alone** unless a rendered frame shows a problem. Every
  template already has a tuned 16:9 and 9:16 layout.
- **Do not clear a meme block's review flag.** Comedy is reviewed by a human.
- **Read `overlapping` in the result.** It names blocks that are on screen at the
  same moment *and* land in the same part of the frame. Different tracks draw one
  over the other; they do not move it. Fix it by retiming one or moving it.

If the user hands you a written shot list rather than asking you to decide, run
it through `apply_directives` instead of translating it yourself. One directive
per line, `[mm:ss] verb description` or `"verbatim quote" verb description`. Any
template id works as the verb (`keyword`, `image-pop`, `stat-pop`, `punch-in`),
matched case-insensitively and never inside the quote. Lines it cannot parse come
back in a review list rather than being dropped — show that list to the user.

## Depth, and the blur

Two per-block properties change where a block sits in the stack rather than what
it draws. Both are ordinary arguments to `place_blocks` and `update_blocks`.

`backdrop: true` frosts the footage and every lower track while the block is on
screen, leaving the block and anything above it sharp. It is what makes an image,
a screenshot or a pulled quote read as sitting *in front of the room* rather than
pasted onto it. The blur fades in and out with the block that owns it, so it can
never be left switched on behind one.

```json
{"template": "image-pop", "quote": "look at this chart",
 "backdrop": true, "fields": {"asset": "chart.png"}}
```

Use it when something on screen has to be read — a chart, a screenshot, a
definition. Do not put it under a keyword that is meant to sit alongside the
speaker rather than replace them. `backdrop-blur` is a template that is nothing
but the frost, for when you want the room softened with nothing over it.

`depth: "behind"` composites a block between the background and the speaker, cut
against the subject matte, so it passes behind their head instead of across it.
It needs that matte, which `set_look(analyze=True)` builds — **check
`speaker_matte` in `get_project` first.** Without it the block is stored as
behind and quietly composites in front, which is the kind of thing you only
notice in the finished video.

## Sound you can take off

`foley` is how much of a template's sound to play. `"off"` is silent, `"full"`
is everything the pack declares, and `"lite"` — the default — is the block
without whatever opens it: a stat card's ticking count without the pop on its
first frame. Reach for `"off"` when several blocks land inside a few seconds
and the edit starts to tick — silencing one is a better answer than deleting a
block that is doing visual work. Whatever is not played leaves the block's
markers off the SFX track too, so what that track shows is what will be heard.

## Breathing, and not stacking on it

The project breathes: a very slow scale drift runs under the whole video on one
clock, so the footage and every graphic move together and no shot is completely
still. It is a project setting (`off`, `subtle`, `standard`, `strong`), and a
`breathe` block overrides the strength over a stretch — including asking for
`off` through a section that should be held still.

A `breathe` block also carries a `rect`, and it is the one thing on that block
you can point at: the breath pulls towards the middle of the box and is never
allowed to crop past its edges. Leave it alone and the block behaves as though
it were not there; move it onto a face and the frame drifts towards the face
instead of the middle. It is a limit on top of the strength, not a second
strength, so the two never fight.

A camera block switches breathing off for its own span automatically, so a
punch-in never rides on a pulse. That is the mechanism, not a licence: still one
camera move at a time.

## The stage

`stage` is a full-frame animated backdrop for an explainer stretch, in the same
desk language as everything else. Its `framing` field decides what happens to
the speaker:

- `"behind"` — the backdrop replaces the room; the speaker stays live and full
  size. Needs the subject matte, same as `depth: "behind"` does.
- `"pip"` — the backdrop takes the frame and the speaker shrinks into a window
  on it, cropped to the project's speaker region.
- `"solo"` — backdrop only, no camera.

Do not also set `depth` on a stage block. The framing *is* the depth, and the
block ignores the toggle.

## The PiP window

`pip-speaker` fills the frame with an image or clip and drops the speaker into a
small rounded window. The window shows the **speaker region** of the footage,
scaled down — not whatever happens to be in that corner of the shot.

Set the region once per project, then look at it:

```
set_speaker_region(project="x", x=0.30, y=0.04, w=0.40, h=0.84)
render_frame(project="x", at=<a moment the PiP is on screen>)
```

Roughly shoulders-up is what reads well. Too tight crops the top of the head off;
too loose leaves the person small with a lot of empty room around them. A single
block can override it with its own `region` field, but a talking head does not
move between shots, so it usually should not.

`entrance` chooses how the window arrives — `lid` swings it up about its hinged
edge with real perspective, the way a laptop opens, and closes it the same way;
it is the default. `pop`, `slide` and `whip` are plainer alternatives. `effects`
adds a glow or a single expanding ring as it lands; both settle to nothing once
it has arrived, so a long PiP is not still pulsing at the viewer.

## Meme text

`meme-frame` takes `top` and `bottom` impact text, and five numbers that
control how they look:

- `text_size` — a multiplier, 0.5 to 2. It is measured against the *image*, not
  the frame, so one setting reads the same on a small meme and a large one. Big
  type wraps onto more lines rather than shrinking back to fit two, so the
  setting means what it says even on a long line; once the text fills the
  picture, raising it further does nothing more.
- `top_x` and `bottom_x` — where each line sits across the picture, 0 flush
  with the left edge and 1 with the right. Both default to 0.5, centred.
- `top_y` and `bottom_y` — where each line sits down the picture, 0 at the
  image's top edge and 1 at its bottom. Defaults are 0 and 1, the classic
  layout.

Every position is clamped against the text's own measured block, so the text
always lands inside the image, whatever size it is and however many lines it
wrapped onto — there is no combination that pushes text off the picture. When a
line is too big to have any room left to move, both ends of its slider centre
it. Use these to move a line off a face rather than to redesign the meme.

## The look

`set_look` gives the whole project a background defocus and an automatic
colour grade. Both are measured from the footage, so there are two amounts and
nothing else to choose.

```
set_look(project="x", analyze=True, defocus=0.6, grade=0.7)
```

`analyze=True` is a one-time pass per project: a depth model reads the whole
source and writes a matte, and the colour is measured from frames across the
timeline. It takes roughly as long as the video and only has to be repeated if
the source is replaced. After that the two amounts apply instantly.

- Offer it when the footage is flat, grey or shot against a busy room. Do not
  switch it on by reflex — footage that already looks good does not need it.
- `defocus` around 0.6 reads like a fast lens; 1.0 is heavy.
- `grade` defaults to 0.7 after an analysis, which is deliberately short of
  full: an auto grade at full strength is what makes footage look processed.
- Check it with `render_frame`. The still includes the look, so what comes back
  is what the export will contain.
- An export with a look on re-encodes every span, so it is slower than a
  lossless export of the same edit. Say so if the user is waiting on it.

## Assets

Blocks that need an image, video or PDF can be placed before the file exists;
they come back flagged `missing`. `add_asset` attaches a local file,
`import_source` brings in footage. For a document, `find_text_in_asset` locates a
phrase and `highlight_lines` draws the marker on it, which is far more reliable
than guessing coordinates.

## Captions

`set_caption_style` sets placement and look **per orientation** — 16:9 and 9:16
are styled separately. `edit_captions` fixes what Whisper misheard (names and
jargon, reliably) and can pin one block away from the global placement with
`pin_to`. A pin also belongs to one orientation only: a caption pinned in 16:9
keeps following the project placement in 9:16 until it is pinned there too.

Rewriting a caption's text drops its word timings, so per-word highlighting stops
for that block. That is intended — the old timings describe the old words.

## Checking your own work

After placing, `render_frame` at three or four moments — including one where two
blocks are close together, and one near the end. Look at the images.

Things that only show up visually: a heading that ran long, a card sitting over
the speaker's face, two graphics overlapping, a caption colliding with a lower
graphic. Fix them with `update_blocks`.

Do not skip this. A coordinate list always looks fine.

## Finishing

- `export` with mode `lossless` unless the user asked for a different resolution;
  `export_status` polls a long one.
- Tell the user where the file is and what you placed, briefly.

## When to ask instead of guessing

- A quote could plausibly refer to two different things.
- A claim seems to need a source card but no source was named.
- The user's brief implies a graphic you have no asset for — place the block
  anyway (it is flagged as awaiting an asset) and say which files are needed.
- The footage needs cutting in a way that changes meaning.

## Reference

- `reference/tools.md` — every tool, grouped
- `docs/MCP.md` in the Editoro repo — full tool documentation
- `docs/TEMPLATES.md` — writing a new template pack
