# Editoro over MCP

`mcp_server.py` exposes the whole editor over the Model Context Protocol. It is a
thin client of Editoro's local HTTP API, so it works with any MCP client and any
model — nothing in it is specific to one vendor.

---

## Running it

```bash
python mcp_server.py                                   # stdio (most clients)
python mcp_server.py --transport streamable-http --port 8766
python mcp_server.py --transport sse --port 8766
python mcp_server.py --print-config                    # paste-ready stdio config
```

Whatever the client, point it at Editoro's own interpreter rather than a
system Python, so the dependencies are the ones `launch.cmd` installed.

### Claude Code

The repo ships a `.mcp.json`, so a clone is already wired up — approve the
server when Claude Code offers it. That config is scoped to this directory,
which is only useful while you are working *in* the repo. To edit video from
anywhere, install it once at user scope instead:

```bash
claude mcp add -s user editoro -- \
  E:/Programming/Editoro/.venv/Scripts/python.exe E:/Programming/Editoro/mcp_server.py
claude mcp list          # editoro: ... - ✔ Connected
```

You do not need the editor running first. The server starts it on the first
tool call and leaves it up, so `/editoro` works from a cold machine.

### Claude Desktop, and every other stdio client

Paste this into the client's MCP config — `claude_desktop_config.json` on
Desktop, `mcp.json` or equivalent elsewhere. `python mcp_server.py
--print-config` prints it with your own paths already filled in:

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

On macOS and Linux the interpreter is `.venv/bin/python`. Forward slashes are
fine on Windows; if you prefer backslashes, escape them (`\\`).

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `EDITORO_URL` | `http://127.0.0.1:8765` | Where the editor is. Point it elsewhere to drive a remote instance. |
| `EDITORO_PORT` | `8765` | Port used to build the default URL. |
| `EDITORO_AUTOSTART` | `1` | Start the editor if it is not running. Set to `0` to require it already be up. |
| `EDITORO_TIMEOUT` | `180` | HTTP timeout in seconds. Transcription and export have their own longer waits. |

The editor is started headless and left running, so the browser UI and the agent
share one live project. An edit made through MCP shows up in an open tab
immediately.

---

## The two conventions that matter

**Time is timeline seconds.** Position in the finished edit, not in the raw file.
Cuts are applied first, so a block at 12.0 s lands twelve seconds into the
export. The one exception is `set_cuts` / `remove_range`, which necessarily talk
about the *source* file; both say so in their descriptions.

**Position is a 0..1 fraction of the frame.** The same numbers work at any
resolution and in both orientations. You rarely need to set them at all — every
template already carries a tuned layout for 16:9 and for 9:16.

---

## A whole edit

```
get_project            what is here, how long, which orientation
get_transcript         what is actually said, with timings
list_templates         what can go on screen, and its fields
remove_range           cut the stumbles out first
place_blocks           the entire edit in one call, anchored to quotes
set_look               defocus the background, grade the picture (analyse once)
render_frame           look at three or four moments
update_blocks          fix what the frames showed
export                 write the MP4
```

Anchor to speech rather than to timestamps. A `quote` still points at the right
moment after you cut something out; a raw `at` does not:

```json
{
  "template": "stat-pop",
  "quote": "about eighty seven percent",
  "duration": 2.5,
  "fields": {"value": "87", "suffix": "%", "label": "of the file"}
}
```

`place_blocks` fills in placement, sizing, both orientation layouts, the
template's default duration and its foley, and puts each block on the first
track where it does not overlap. Unknown templates, unknown fields, quotes that
are not in the transcript and two camera moves at the same time all come back as
named problems rather than being silently dropped.

---

## Tools

### Reading

| Tool | What it is for |
|---|---|
| `diagnostics` | FFmpeg, GPU encoder and template health. Any pack that failed to load is named here. |
| `list_projects` | Every project with its footage and length. |
| `get_project` | Source, duration, orientation, cuts, caption count, and everything on the timeline. |
| `list_templates` | The catalogue. Field names, types, defaults and what each one means. Call it once per session. |
| `get_transcript` | `blocks`, `text`, `srt` or per-word timings. |
| `find_quote` | Where something was said. Ignores punctuation and Arabic diacritics, and matches across caption boundaries. |
| `list_assets`, `get_asset_info` | What media exists, and its real pixel dimensions. |
| `find_text_in_asset` | Where text sits on a PDF page, in 0..1 coordinates. |
| `export_status` | Finished exports, and the last failure if there was one. |

### Writing

| Tool | What it is for |
|---|---|
| `create_project`, `import_source`, `add_asset` | Set a project up. |
| `generate_captions`, `caption_status` | Local Whisper with word timings. |
| `place_blocks` | Place the edit. The main tool. |
| `update_blocks` | Retime, restyle, fill in a missing asset, delete. |
| `clear_timeline` | Remove all blocks, or only those of some templates. |
| `apply_directives` | Parse a written directive list and populate the timeline from it. |
| `highlight_lines` | Place a highlighted document page, with the strokes resolved from the real page text. |
| `edit_captions` | Fix mistranscriptions, retime, pin one caption somewhere. |
| `set_caption_style` | Placement, grouping, size, active-word highlight — per orientation. |
| `set_cuts`, `remove_range` | Cut the source down. |
| `set_orientation` | Choose which of each template's two layouts is used. |
| `set_look` | Project-wide background defocus and automatic colour, both measured from the footage. |
| `set_speaker_region` | Where the speaker is in the source frame. Set once; PiP windows crop to it. |
| `set_notes` | Leave an editing plan on the project. |

### Per-block properties `place_blocks` and `update_blocks` both take

| Property | What it does |
|---|---|
| `backdrop` | Frost the footage and every lower track while this block is on screen. |
| `depth` | `front` or `behind` the speaker. `behind` needs the subject matte — check `speaker_matte` in `get_project` first. |
| `silent` | Place the block without its template's foley. Use it when several blocks land within a few seconds and the edit starts to tick; it is a better answer than deleting a block that is doing visual work. |
| `tilt` | `off`, `left`, `right` or `lean`. A small out-of-plane lean so the block reads as an object on the scene. Tuned presets, not an angle. Combines with `depth`. |

Two things the timeline does on its own are worth knowing about before you place
anything. The project **breathes** — a very slow scale drift on one shared clock
under the footage and every graphic — and a camera block switches it off for its
own span, which is another reason not to stack camera moves. And the **stage**
template is a full-frame animated backdrop whose `framing` field (`behind`,
`pip`, `solo`) decides what happens to the speaker; do not set `depth` on it, the
framing already is the depth.

### Looking

| Tool | What it is for |
|---|---|
| `render_frame` | One real composited frame, as an image. |
| `export` | Render the finished video. |

`render_frame` runs the same renderer the export uses, so what comes back is a
frame of the finished video rather than a preview of it. Overlapping cards, text
that ran longer than the designer expected, or a graphic sitting over the
speaker's face are obvious in a still and invisible in a coordinate list. Use it
before exporting.

---

## Resources and prompts

| URI | Contents |
|---|---|
| `editoro://templates` | The full catalogue, including both orientation layouts. |
| `editoro://projects` | Every project. |
| `editoro://projects/{project}/transcript` | The timed transcript as readable text. |
| `editoro://projects/{project}/timeline` | Everything currently placed. |

One prompt, `edit_video`, contains a working order for a full pass.

---

## Editorial rules the server assumes

These are enforced where they can be and stated in the tool descriptions where
they cannot:

- **Keyword and quote text is verbatim.** These templates exist to pin words the
  speaker actually said. Paraphrasing them is the fastest way to make a video
  look automated.
- **One camera move at a time.** `place_blocks` refuses a second overlapping
  camera template, because the export can only honour one.
- **Meme blocks stay flagged.** They are placed with a review flag on purpose.
- **Leave placement alone unless there is a reason.** The layouts are tuned.
- **Silence is allowed.** A graphic every few seconds is decoration, not editing.
- **Blur belongs to a block, not to the timeline.** `backdrop` on a block frosts
  the footage and every lower track while that block is on screen, and leaves
  the block itself and everything above it sharp. It fades in and out with the
  block that owns it, so it cannot be left switched on behind one. Use it to put
  an image, a screenshot or a quote *in front of the room* rather than pasted
  onto it. `backdrop-blur` is the same thing with nothing drawn on top.
- **Depth is a toggle, not a track order.** `depth: "behind"` composites a block
  between the background and the speaker, cut against the subject matte, so it
  passes behind their head. Track order everywhere else in Editoro means
  time-and-stack; it does not also mean depth. `behind` needs the matte, which
  is built the first time `set_look` analyses the footage — check
  `speaker_matte` in `get_project` before relying on it, because without it the
  block simply stays in front.
- **The PiP window shows the speaker region, not the corner it sits in.** Set
  `set_speaker_region` once per project and look at a `render_frame`. A block
  can override it with its own `region`, but a talking head does not move
  between shots, so it usually should not.
- **Separate tracks are not separate places.** `place_blocks` returns
  `overlapping` when two blocks are on screen at the same moment *and* land in
  the same part of the frame. Different tracks put one over the other; they do
  not move it.
- **The look is measured, not chosen.** `set_look` has two amounts and no other
  controls, because everything else about it was decided by reading the
  footage. Analysing is a one-time pass per project and takes roughly as long
  as the video; the amounts afterwards are instant.

---

## Testing it

```bash
.venv\Scripts\python.exe tools/test_mcp.py
```

Builds a synthetic project and drives it entirely through the MCP tools —
transcript, quote anchoring, placement, orientation switching, caption edits,
cutting, a rendered frame and a real export — then deletes it. If it passes, an
agent can complete an edit without touching the UI.
