#!/usr/bin/env python3
"""Editoro MCP server - drive the whole editor from any MCP client.

Editoro is a local, template-driven video editor. This process exposes it over
the Model Context Protocol so a model can read a video's transcript, decide what
should appear on screen and when, place it, look at the result, and export -
without a human moving anything by hand.

Run it:

    python mcp_server.py                       # stdio (Claude Desktop, most clients)
    python mcp_server.py --transport streamable-http --port 8766
    python mcp_server.py --transport sse --port 8766

It is a thin, spec-compliant client of Editoro's local HTTP API, so nothing here
is tied to one MCP client or one model. If the editor is not already running it
is started automatically and left running, which means the browser UI and the
agent are always looking at the same project: an edit made here shows up live in
an open tab, and an edit made by hand is visible to the next tool call.

Design notes that matter when reading the tools below:

* Time is always **timeline seconds** - position in the edit, not in the raw
  source file. Cuts are applied before anything else, so a template placed at
  12.0 s lands 12 seconds into the finished video.
* Positions are normalised 0..1 fractions of the frame, so the same numbers work
  at any resolution and in both orientations.
* Every template ships a 16:9 and a 9:16 layout. Placement defaults come from
  whichever one matches the footage; you rarely need to set coordinates at all.
* Anchoring to speech is almost always better than guessing a timestamp: give
  `place_blocks` a `quote` instead of an `at`, and use `find_quote` to check.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent.resolve()
DEFAULT_PORT = int(os.environ.get("EDITORO_PORT", "8765"))
BASE_URL = os.environ.get("EDITORO_URL", f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")
AUTOSTART = os.environ.get("EDITORO_AUTOSTART", "1") != "0"
REQUEST_TIMEOUT = float(os.environ.get("EDITORO_TIMEOUT", "180"))


# --------------------------------------------------------------------- client
class EditoroError(RuntimeError):
    """An error worth showing the model verbatim - it says what to do next."""


class Editoro:
    """Small async client for the local Editoro API."""

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url
        self._client: Optional[httpx.AsyncClient] = None
        self._process: Optional[subprocess.Popen] = None
        self._lock = asyncio.Lock()

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=REQUEST_TIMEOUT)
        return self._client

    async def _alive(self) -> bool:
        try:
            client = await self.client()
            response = await client.get("/api/health", timeout=2.0)
            return response.status_code == 200 and response.json().get("app") == "Editoro"
        except Exception:
            return False

    async def ensure_running(self) -> None:
        """Start the editor if it is not already up, then wait for it."""
        async with self._lock:
            if await self._alive():
                return
            if not AUTOSTART:
                raise EditoroError(
                    f"Editoro is not running at {self.base_url}. Start it with launch.cmd, "
                    "or set EDITORO_AUTOSTART=1 to let this server start it."
                )
            script = ROOT / "server.py"
            if not script.is_file():
                raise EditoroError(
                    f"Cannot find server.py next to {__file__}. Set EDITORO_URL to a "
                    "running Editoro instead."
                )
            interpreter = _python_for_editoro()
            self._process = subprocess.Popen(
                [str(interpreter), str(script), "--no-browser"],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if await self._alive():
                    return
                if self._process.poll() is not None:
                    raise EditoroError(
                        "Editoro exited while starting. Run launch.cmd once so its "
                        "dependencies and export renderer are installed."
                    )
                await asyncio.sleep(0.4)
            raise EditoroError("Editoro did not become ready within 60 seconds.")

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        await self.ensure_running()
        client = await self.client()
        response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            detail: Any
            try:
                payload = response.json()
                detail = payload.get("detail") or payload.get("message") or payload
            except Exception:
                detail = response.text[:600]
            raise EditoroError(f"{method} {path} failed ({response.status_code}): {detail}")
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.content

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)

    async def state(self, project: str) -> dict[str, Any]:
        return await self.get(f"/api/projects/{project}/state")

    async def save(self, state: dict[str, Any]) -> dict[str, Any]:
        result = await self.post(f"/api/projects/{state['name']}/state", json=state)
        return result.get("state", state)

    async def packs(self) -> dict[str, dict[str, Any]]:
        payload = await self.get("/api/templates")
        return {pack["id"]: pack for pack in payload["packs"]}


def _python_for_editoro() -> Path:
    """Prefer Editoro's own virtual environment; it has FFmpeg's companions."""
    for candidate in (
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


EDITORO = Editoro()


# ----------------------------------------------------------------- utilities
def _new_id(prefix: str) -> str:
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _timeline_duration(state: dict[str, Any]) -> float:
    kept = sum(cut["src_out"] - cut["src_in"] for cut in state.get("cuts", []))
    return round(kept or state.get("source_info", {}).get("duration", 0.0), 4)


def _brief_instance(item: dict[str, Any]) -> dict[str, Any]:
    """One placed block, small enough to list a hundred of them."""
    summary = {
        "id": item["id"], "template": item["template"], "track": item["track"],
        "start": round(item["start"], 3), "duration": round(item["duration"], 3),
        "x": round(item["x"], 3), "y": round(item["y"], 3),
        "scale": round(item["scale"], 3),
    }
    text = item.get("fields", {}).get("text") or item.get("fields", {}).get("title")
    if text:
        summary["text"] = str(text)[:60]
    if item.get("missing"):
        summary["missing_asset"] = True
    if item.get("review"):
        summary["needs_review"] = True
    return summary


def _describe_field(field: dict[str, Any]) -> dict[str, Any]:
    described = {
        "name": field["name"], "type": field["type"],
        "description": field.get("description") or field.get("note") or "",
    }
    for key in ("default", "options", "min", "max", "step", "max_length"):
        if key in field:
            described[key] = field[key]
    return described


def _describe_pack(pack: dict[str, Any], full: bool = False) -> dict[str, Any]:
    described = {
        "id": pack["id"],
        "name": pack.get("display_name", pack["id"]),
        "category": pack.get("category", ""),
        "description": pack.get("description", ""),
        "default_duration": pack.get("default_duration"),
        "fields": [_describe_field(field) for field in pack.get("fields", [])],
    }
    if pack.get("camera"):
        described["camera_mode"] = pack["camera"]["mode"]
        described["moves_the_footage"] = True
    if pack.get("full_frame"):
        described["full_frame"] = True
    if pack.get("directive_verb"):
        described["directive_verb"] = pack["directive_verb"]
    if full:
        described["layouts"] = {
            orientation: {
                key: value for key, value in (pack.get("_variants", {}).get(orientation, {})).items()
                if key in {"x", "y", "scale", "max_width", "max_height"}
            }
            for orientation in ("horizontal", "vertical")
        }
        described["colour"] = pack.get("category_color")
        described["sounds"] = [sound["file"].rsplit("/", 1)[-1] for sound in pack.get("sfx", [])]
    return described


async def _resolve_placement(
    packs: dict[str, dict[str, Any]], state: dict[str, Any], template: str,
    x: Optional[float], y: Optional[float], scale: Optional[float],
) -> dict[str, dict[str, float]]:
    """Placement for both orientations, defaulting to the pack's own layouts."""
    pack = packs[template]
    layouts: dict[str, dict[str, float]] = {}
    for orientation in ("horizontal", "vertical"):
        variant = pack.get("_variants", {}).get(orientation, {})
        layouts[orientation] = {
            "x": float(variant.get("x", 0.5)),
            "y": float(variant.get("y", 0.3)),
            "scale": float(variant.get("scale", 1.0)),
        }
    active = state.get("orientation", "horizontal")
    if x is not None:
        layouts[active]["x"] = max(0.0, min(1.0, x))
    if y is not None:
        layouts[active]["y"] = max(0.0, min(1.0, y))
    if scale is not None:
        layouts[active]["scale"] = max(0.05, min(10.0, scale))
    return layouts


def _free_track(state: dict[str, Any], start: float, duration: float,
                reserved: list[dict[str, Any]]) -> int:
    """Lowest track where this block does not sit on top of another one."""
    existing = list(state.get("instances", [])) + reserved
    for track in range(1, 65):
        clash = any(
            item["track"] == track
            and item["start"] < start + duration
            and start < item["start"] + item["duration"]
            for item in existing
        )
        if not clash:
            return track
    return 64


# ------------------------------------------------------------------- server
mcp = MCPServer(
    name="editoro",
    title="Editoro",
    version="2.0.0",
    instructions=(
        "Editoro is a local, template-driven video editor for talking-head videos.\n\n"
        "A normal editing pass looks like this:\n"
        "  1. list_projects / create_project, then import_source and generate_captions.\n"
        "  2. get_transcript to read what is actually said.\n"
        "  3. list_templates once, to see what can go on screen and what fields it takes.\n"
        "  4. place_blocks - anchor each block to a spoken quote rather than a timestamp\n"
        "     wherever you can, and place the whole edit in one call.\n"
        "  5. render_frame at a few moments to check your own work visually.\n"
        "  6. export.\n\n"
        "Rules that keep the result looking like one video rather than a pile of effects:\n"
        "  * Keyword and quote text must be words the speaker actually said, verbatim.\n"
        "  * Do not stack camera moves; one punch-in, zoom-out or ken-burns at a time.\n"
        "  * Leave placement to the template unless there is a reason to move it. Every\n"
        "    template already has a tuned 16:9 and 9:16 layout.\n"
        "  * Meme blocks are flagged for human review on purpose. Do not clear that flag.\n"
        "  * Silence is allowed. A block every few seconds is decoration, not editing."
    ),
)


# --------------------------------------------------------------- discovery
@mcp.tool()
async def diagnostics() -> dict[str, Any]:
    """Check that Editoro, FFmpeg, the GPU encoder and every template pack are healthy.

    Worth calling once at the start of a session: it reports any template that
    failed to load, which is the one failure that silently removes options.
    """
    health = await EDITORO.get("/api/diagnostics")
    return {
        "editoro_url": EDITORO.base_url,
        "ffmpeg": health.get("ffmpeg"),
        "ffprobe": health.get("ffprobe"),
        "export_renderer": health.get("renderer"),
        "gpu_encoder": health.get("nvenc"),
        "templates_loaded": health.get("templates"),
        "template_errors": health.get("template_errors") or {},
    }


@mcp.tool()
async def list_projects() -> dict[str, Any]:
    """List every project, with its source footage, length and orientation."""
    payload = await EDITORO.get("/api/projects")
    return {"projects": payload["projects"]}


@mcp.tool()
async def create_project(
    name: Annotated[str, Field(description="Letters, numbers, hyphens and underscores only.")],
) -> dict[str, Any]:
    """Create an empty project folder and open it."""
    await EDITORO.post(f"/api/projects/{name}")
    return {"created": name, "next": "import_source, then generate_captions"}


@mcp.tool()
async def get_project(
    project: str,
    include_timeline: Annotated[bool, Field(
        description="Include every placed block. Leave off for a quick overview.")] = True,
) -> dict[str, Any]:
    """Everything you need to know about a project before editing it.

    Source dimensions, length, orientation, how many captions exist, and what is
    already on the timeline.
    """
    state = await EDITORO.state(project)
    info = state.get("source_info", {})
    summary: dict[str, Any] = {
        "project": project,
        "source": state.get("source") or None,
        "resolution": f"{info.get('width')}x{info.get('height')}" if info.get("width") else None,
        "fps": info.get("fps"),
        "source_duration": round(info.get("duration", 0), 3),
        "timeline_duration": _timeline_duration(state),
        "orientation": state.get("orientation"),
        "orientation_mode": state.get("orientation_mode", "auto"),
        "cuts": [{"src_in": round(cut["src_in"], 3), "src_out": round(cut["src_out"], 3)}
                 for cut in state.get("cuts", [])],
        "caption_blocks": len(state.get("captions", [])),
        "has_word_timings": any(block.get("words") for block in state.get("captions", [])),
        "blocks_placed": len(state.get("instances", [])),
        "caption_style": state.get("caption_style", {}),
        "notes": state.get("notes", ""),
    }
    if include_timeline:
        summary["timeline"] = [_brief_instance(item) for item in state.get("instances", [])]
        summary["needs_assets"] = [item["id"] for item in state.get("instances", [])
                                   if item.get("missing")]
        summary["needs_review"] = [item["id"] for item in state.get("instances", [])
                                   if item.get("review")]
    return summary


@mcp.tool()
async def list_templates(
    category: Annotated[str, Field(
        description="Filter by category: text, image, video, annotation, camera, screen, "
                    "structure, ambient, captions. Empty lists them all.")] = "",
    detailed: Annotated[bool, Field(
        description="Include per-orientation layouts, colours and sounds.")] = False,
) -> dict[str, Any]:
    """Every template that can be placed, with its fields and what it is for.

    Call this once per session before placing anything. The `fields` list is the
    contract for `place_blocks`: field names, types, defaults and what each one
    means. Templates with `moves_the_footage` transform the video itself rather
    than drawing over it, and only one of those can be active at a time.
    """
    packs = await EDITORO.packs()
    chosen = [
        pack for pack in packs.values()
        if not category or pack.get("category", "") == category
    ]
    chosen.sort(key=lambda pack: (pack.get("category", ""), pack["id"]))
    return {
        "count": len(chosen),
        "categories": sorted({pack.get("category", "") for pack in packs.values()}),
        "templates": [_describe_pack(pack, full=detailed) for pack in chosen],
    }


# --------------------------------------------------------------- transcript
@mcp.tool()
async def get_transcript(
    project: str,
    format: Annotated[Literal["blocks", "text", "srt", "words"], Field(
        description="blocks = timed caption blocks (default and usually what you want); "
                    "text = one plain string; srt = subtitle file text; "
                    "words = every word with its own timing.")] = "blocks",
) -> dict[str, Any]:
    """Read what is actually said in the video, with timings.

    This is the input to every editing decision: place things where the words
    that justify them are spoken. `blocks` gives readable, timestamped lines;
    `words` gives per-word timings when you need frame-level precision.
    """
    payload = await EDITORO.get(f"/api/projects/{project}/transcript")
    blocks = payload["blocks"]
    if not blocks and not payload["words"]:
        return {
            "project": project, "blocks": [],
            "note": ("No transcript yet. Run generate_captions, or import an .srt / "
                     "Whisper .json file through the editor."),
        }
    if format == "text":
        return {"project": project, "duration": payload["duration"], "text": payload["text"]}
    if format == "words":
        return {"project": project, "duration": payload["duration"],
                "words": [{"w": word["w"], "s": round(word["s"], 3), "e": round(word["e"], 3)}
                          for word in payload["words"]]}
    if format == "srt":
        def stamp(seconds: float) -> str:
            ms = int(round(seconds * 1000))
            return (f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:"
                    f"{ms // 1000 % 60:02d},{ms % 1000:03d}")
        lines = []
        for index, block in enumerate(blocks, start=1):
            lines.append(f"{index}\n{stamp(block['start'])} --> {stamp(block['end'])}\n"
                         f"{block['text']}\n")
        return {"project": project, "srt": "\n".join(lines)}
    return {
        "project": project,
        "duration": payload["duration"],
        "blocks": [{"id": block["id"], "start": round(block["start"], 3),
                    "end": round(block["end"], 3), "text": block["text"]}
                   for block in blocks],
    }


@mcp.tool()
async def find_quote(
    project: str,
    quote: Annotated[str, Field(description="Words as they were spoken. Punctuation and "
                                            "Arabic diacritics are ignored when matching.")],
    limit: int = 5,
) -> dict[str, Any]:
    """Find when something was said, in timeline seconds.

    Prefer this over guessing timestamps. It matches across caption boundaries
    and tolerates punctuation differences, so a phrase copied out of the
    transcript still lands where it was spoken.
    """
    payload = await EDITORO.get(f"/api/projects/{project}/find-quote",
                                params={"q": quote, "limit": limit})
    if not payload["matches"]:
        return {"query": quote, "matches": [],
                "note": "Nothing matched. Check the wording against get_transcript."}
    return payload


@mcp.tool()
async def generate_captions(
    project: str,
    model: Annotated[Literal["large-v3", "large-v3-turbo"], Field(
        description="large-v3 is the accuracy default; turbo is roughly twice as fast.")]
        = "large-v3",
    language: Annotated[Literal["auto", "en", "ar"], Field(
        description="Spoken language, or auto to detect it.")] = "auto",
    terms: Annotated[str, Field(
        description="Names and jargon that Whisper usually gets wrong, comma separated.")] = "",
    wait: Annotated[bool, Field(
        description="Block until transcription finishes. It runs locally on the GPU and a "
                    "long video can take several minutes.")] = True,
) -> dict[str, Any]:
    """Transcribe the source video locally with Whisper, with word-level timings.

    Nothing leaves the machine. Existing captions are only replaced once a run
    succeeds, so a failed or cancelled job cannot destroy work.
    """
    started = await EDITORO.post(f"/api/projects/{project}/transcribe",
                                 json={"model": model, "language": language, "terms": terms})
    if not wait:
        return {"status": started.get("status"), "note": "Poll with caption_status."}
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        status = await EDITORO.get(f"/api/projects/{project}/transcribe")
        if status.get("status") in {"complete", "failed", "canceled", "idle"}:
            if status.get("status") == "failed":
                raise EditoroError(f"Transcription failed: {status.get('error')}")
            return {"status": status.get("status"),
                    "caption_blocks": status.get("caption_count"),
                    "device": status.get("device"),
                    "next": "get_transcript, then place_blocks"}
        await asyncio.sleep(2.0)
    raise EditoroError("Transcription did not finish within an hour.")


@mcp.tool()
async def caption_status(project: str) -> dict[str, Any]:
    """Progress of a running local transcription."""
    return await EDITORO.get(f"/api/projects/{project}/transcribe")


@mcp.tool()
async def set_caption_style(
    project: str,
    position: Annotated[Literal["", "upper", "center", "lower"], Field(
        description="Vertical preset for the current orientation. Empty leaves it alone.")] = "",
    grouping: Annotated[Literal["", "chunk", "sentence"], Field(
        description="chunk shows a few words at a time; sentence holds a whole sentence.")] = "",
    words_per_chunk: Annotated[int, Field(
        description="Words per caption in chunk mode. 0 leaves it alone.", ge=0, le=24)] = 0,
    font_size: Annotated[float, Field(
        description="Caption size in 1080p units. 0 leaves it alone.", ge=0, le=160)] = 0,
    highlight_active_word: Annotated[Optional[bool], Field(
        description="Lift the word currently being spoken. Needs word timings.")] = None,
    backing_box: Annotated[Optional[bool], Field(
        description="Draw a dark box behind the text.")] = None,
    regroup: Annotated[bool, Field(
        description="Rebuild the caption blocks from word timings using the new grouping. "
                    "Blocks whose text was hand-edited are left untouched.")] = False,
) -> dict[str, Any]:
    """Set how captions look and where they sit, per orientation.

    Placement is kept separately for 16:9 and 9:16, and a caption that was
    dragged somewhere specific keeps its own spot until it is released.
    """
    state = await EDITORO.state(project)
    style = state.setdefault("caption_style", {})
    orientation = state.get("orientation", "horizontal")
    layout = style.setdefault(orientation, {})
    if position:
        layout["y"] = {"upper": 0.16, "center": 0.5,
                       "lower": 0.78 if orientation == "vertical" else 0.86}[position]
        layout["x"] = 0.5
        style["preset"] = position
    if grouping:
        style["mode"] = grouping
    if words_per_chunk:
        style["words_per_chunk"] = words_per_chunk
    if font_size:
        style["font_size"] = font_size
    if highlight_active_word is not None:
        style["highlight_active_word"] = highlight_active_word
    if backing_box is not None:
        style["box"] = backing_box
    state = await EDITORO.save(state)
    if regroup:
        state = await EDITORO.post(
            f"/api/projects/{project}/captions/regroup",
            json={"mode": style.get("mode", "chunk"),
                  "words_per_chunk": style.get("words_per_chunk", 5)},
        )
    return {"caption_style": state.get("caption_style"),
            "caption_blocks": len(state.get("captions", []))}


class CaptionEdit(BaseModel):
    """One correction to one caption block."""
    id: Annotated[str, Field(description="Block id from get_transcript.")]
    text: Annotated[Optional[str], Field(
        description="Corrected wording. Setting this clears the block's word timings.")] = None
    start: Annotated[Optional[float], Field(description="New start, timeline seconds.")] = None
    end: Annotated[Optional[float], Field(description="New end, timeline seconds.")] = None
    delete: Annotated[bool, Field(description="Remove this block entirely.")] = False
    pin_to: Annotated[Optional[list[float]], Field(
        description="[x, y] in 0..1 to pin this one caption; it stops following the "
                    "project placement. Pass an empty list to release it.")] = None


@mcp.tool()
async def edit_captions(
    project: str,
    edits: Annotated[list[CaptionEdit], Field(description="Corrections to apply together.")],
) -> dict[str, Any]:
    """Fix mistranscriptions, retime captions, or pin one somewhere specific.

    Whisper reliably mangles names and jargon; this is how you correct them
    without touching anything else.
    """
    state = await EDITORO.state(project)
    blocks = {block["id"]: block for block in state.get("captions", [])}
    changed, missing = 0, []
    removals: set[str] = set()
    for edit in edits:
        block = blocks.get(edit.id)
        if block is None:
            missing.append(edit.id)
            continue
        if edit.delete:
            removals.add(edit.id)
            changed += 1
            continue
        if edit.text is not None:
            block["text"] = edit.text
            # Word timings describe the words Whisper heard. Once the text is
            # rewritten they no longer line up, so they go rather than drift.
            block["words"] = []
        if edit.start is not None:
            block["start"] = float(edit.start)
        if edit.end is not None:
            block["end"] = float(edit.end)
        if edit.pin_to is not None:
            if len(edit.pin_to) >= 2:
                block["follow_global"] = False
                block["x"], block["y"] = float(edit.pin_to[0]), float(edit.pin_to[1])
            else:
                block["follow_global"] = True
                block["x"] = block["y"] = block["scale"] = None
        changed += 1
    state["captions"] = [block for block in state["captions"] if block["id"] not in removals]
    state = await EDITORO.save(state)
    result: dict[str, Any] = {"updated": changed, "caption_blocks": len(state["captions"])}
    if missing:
        result["unknown_ids"] = missing
    return result


# ------------------------------------------------------------------ placing
class Block(BaseModel):
    """One template instance to place on the timeline."""

    template: Annotated[str, Field(
        description="Template id from list_templates, e.g. keyword, image-pop, stat-pop.")]
    at: Annotated[Optional[float], Field(
        description="Start time in timeline seconds. Use this or `quote`, not both.")] = None
    quote: Annotated[Optional[str], Field(
        description="Verbatim spoken words to anchor to. The block starts where they are "
                    "said. Preferred over `at` - it survives re-cutting.")] = None
    quote_offset: Annotated[float, Field(
        description="Seconds to shift relative to the quote. Negative starts earlier.")] = 0.0
    duration: Annotated[Optional[float], Field(
        description="Seconds on screen. Defaults to the template's own duration.")] = None
    fields: Annotated[dict[str, Any], Field(
        description="Field values for this template, keyed by field name from "
                    "list_templates. Unset fields fall back to their defaults.")] = {}
    x: Annotated[Optional[float], Field(
        description="Horizontal centre, 0..1. Omit to use the template's layout.")] = None
    y: Annotated[Optional[float], Field(
        description="Vertical centre, 0..1. Omit to use the template's layout.")] = None
    scale: Annotated[Optional[float], Field(
        description="Size multiplier. Omit to use the template's layout.")] = None
    track: Annotated[Optional[int], Field(
        description="Overlay track, 1 and up. Omit to place it on the first free one.")] = None


@mcp.tool()
async def place_blocks(
    project: str,
    blocks: Annotated[list[Block], Field(
        description="Every block to place. Send the whole edit in one call rather than "
                    "one call per block.")],
    replace_existing: Annotated[bool, Field(
        description="Clear the timeline first. Cuts and captions are kept.")] = False,
) -> dict[str, Any]:
    """Place template blocks on the timeline - the main editing tool.

    Anchor blocks to quotes wherever possible: `quote` survives later re-cutting
    in a way that a raw timestamp does not. Placement, sizing and the 16:9/9:16
    layouts are filled in from the template unless you override them, and each
    block lands on the first track where it does not overlap something else.

    Unknown fields and unknown templates are reported rather than silently
    dropped, so a partially wrong call tells you exactly what to fix.
    """
    packs = await EDITORO.packs()
    state = await EDITORO.state(project)
    if not state.get("source"):
        raise EditoroError("Import source footage before placing templates.")
    if replace_existing:
        state["instances"] = []
    duration = _timeline_duration(state)
    fps = max(1.0, state.get("source_info", {}).get("fps") or 30)
    frame = 1.0 / fps

    created: list[dict[str, Any]] = []
    problems: list[str] = []
    camera_ids = {pack_id for pack_id, pack in packs.items() if pack.get("camera")}

    for index, block in enumerate(blocks):
        pack = packs.get(block.template)
        if pack is None:
            problems.append(f"[{index}] unknown template {block.template!r}; "
                            f"call list_templates for the valid ids")
            continue
        start = block.at
        if block.quote:
            found = await EDITORO.get(f"/api/projects/{project}/find-quote",
                                      params={"q": block.quote, "limit": 1})
            if not found["matches"]:
                problems.append(f"[{index}] {block.template}: could not find the quote "
                                f"{block.quote!r} in the transcript")
                continue
            start = found["matches"][0]["start"]
        if start is None:
            problems.append(f"[{index}] {block.template}: give either `at` or `quote`")
            continue
        start = max(0.0, min(float(start) + block.quote_offset, max(0.0, duration - frame)))
        length = float(block.duration or pack.get("default_duration") or 3.0)
        length = max(frame, min(length, max(frame, duration - start)))

        known = {field["name"]: field for field in pack.get("fields", [])}
        fields: dict[str, Any] = {}
        for field in pack.get("fields", []):
            if "default" in field:
                fields[field["name"]] = json.loads(json.dumps(field["default"]))
            elif field["type"] == "strokes":
                fields[field["name"]] = []
            elif field["type"] in {"text", "textarea"}:
                fields[field["name"]] = ""
        for key, value in (block.fields or {}).items():
            if key not in known:
                problems.append(
                    f"[{index}] {block.template}: no field {key!r} "
                    f"(has: {', '.join(sorted(known)) or 'none'})")
                continue
            fields[key] = value
        needs_asset = any(
            field["type"].startswith("asset") and not fields.get(field["name"])
            for field in pack.get("fields", [])
        )

        layouts = await _resolve_placement(packs, state, block.template,
                                           block.x, block.y, block.scale)
        active = layouts[state.get("orientation", "horizontal")]
        track = block.track or _free_track(state, start, length, created)
        if block.template in camera_ids:
            # Two camera moves at once fight over the same footage; the export
            # can only honour one, so refuse rather than produce a surprise.
            overlapping = [
                item for item in list(state.get("instances", [])) + created
                if item["template"] in camera_ids
                and item["start"] < start + length and start < item["start"] + item["duration"]
            ]
            if overlapping:
                problems.append(
                    f"[{index}] {block.template}: overlaps the camera move "
                    f"{overlapping[0]['id']}; only one can be active at a time")
                continue
        created.append({
            "id": _new_id("i"), "template": block.template, "track": track,
            "start": round(start, 4), "duration": round(length, 4),
            "x": active["x"], "y": active["y"], "scale": active["scale"],
            "layouts": layouts, "fields": fields,
            "missing": needs_asset,
            "review": bool(pack.get("directive_review")),
        })

    state.setdefault("instances", []).extend(created)
    state = await EDITORO.save(state)
    result: dict[str, Any] = {
        "placed": len(created),
        "blocks": [_brief_instance(item) for item in created],
        "timeline_blocks": len(state.get("instances", [])),
    }
    if problems:
        result["problems"] = problems
    waiting = [item["id"] for item in created if item.get("missing")]
    if waiting:
        result["awaiting_assets"] = waiting
        result["note"] = ("These blocks have no media yet. Use add_asset then "
                          "update_blocks to fill them in.")
    return result


class BlockUpdate(BaseModel):
    """A change to one already-placed block."""
    id: Annotated[str, Field(description="Block id from get_project or place_blocks.")]
    start: Annotated[Optional[float], Field(description="New start, timeline seconds.")] = None
    duration: Annotated[Optional[float], Field(description="New length in seconds.")] = None
    fields: Annotated[Optional[dict[str, Any]], Field(
        description="Field values to merge in. Omitted fields keep their values.")] = None
    x: Annotated[Optional[float], Field(description="New horizontal centre, 0..1.")] = None
    y: Annotated[Optional[float], Field(description="New vertical centre, 0..1.")] = None
    scale: Annotated[Optional[float], Field(description="New size multiplier.")] = None
    track: Annotated[Optional[int], Field(description="New overlay track.")] = None
    delete: Annotated[bool, Field(description="Remove this block.")] = False


@mcp.tool()
async def update_blocks(
    project: str,
    updates: Annotated[list[BlockUpdate], Field(description="Changes to apply together.")],
) -> dict[str, Any]:
    """Retime, restyle, fill in or delete blocks that are already placed.

    Setting an asset field on a block that was waiting for media also clears its
    missing-asset flag, so filling a placeholder is a single call.
    """
    packs = await EDITORO.packs()
    state = await EDITORO.state(project)
    items = {item["id"]: item for item in state.get("instances", [])}
    removals: set[str] = set()
    changed, missing = 0, []
    for update in updates:
        item = items.get(update.id)
        if item is None:
            missing.append(update.id)
            continue
        if update.delete:
            removals.add(update.id)
            changed += 1
            continue
        pack = packs.get(item["template"], {})
        if update.start is not None:
            item["start"] = max(0.0, float(update.start))
        if update.duration is not None:
            item["duration"] = max(0.01, float(update.duration))
        if update.fields:
            known = {field["name"] for field in pack.get("fields", [])}
            unknown = set(update.fields) - known
            if unknown:
                missing.append(f"{update.id}: unknown fields {', '.join(sorted(unknown))}")
            item.setdefault("fields", {}).update(
                {key: value for key, value in update.fields.items() if key in known})
            item["missing"] = any(
                field["type"].startswith("asset") and not item["fields"].get(field["name"])
                for field in pack.get("fields", [])
            )
        orientation = state.get("orientation", "horizontal")
        layouts = item.setdefault("layouts", {})
        for key, value in (("x", update.x), ("y", update.y), ("scale", update.scale)):
            if value is not None:
                item[key] = float(value)
                layouts.setdefault(orientation, {})[key] = float(value)
        if update.track is not None:
            item["track"] = max(1, min(64, int(update.track)))
        changed += 1
    state["instances"] = [item for item in state["instances"] if item["id"] not in removals]
    state = await EDITORO.save(state)
    result: dict[str, Any] = {"updated": changed,
                              "timeline_blocks": len(state.get("instances", []))}
    if missing:
        result["problems"] = missing
    return result


@mcp.tool()
async def clear_timeline(
    project: str,
    templates: Annotated[list[str], Field(
        description="Only remove blocks of these template ids. Empty removes all of them.")] = [],
) -> dict[str, Any]:
    """Remove placed blocks. Cuts, captions and media are left alone."""
    state = await EDITORO.state(project)
    before = len(state.get("instances", []))
    if templates:
        state["instances"] = [item for item in state["instances"]
                              if item["template"] not in set(templates)]
    else:
        state["instances"] = []
    state = await EDITORO.save(state)
    return {"removed": before - len(state["instances"]),
            "remaining": len(state["instances"])}


@mcp.tool()
async def apply_directives(
    project: str,
    text: Annotated[str, Field(
        description="Plain-text directive list in the da7ee7-director grammar: verbatim "
                    "quote anchors, Arabic verbs and timestamps.")],
) -> dict[str, Any]:
    """Parse a written directive list and populate the timeline from it in one action.

    This is the paste-a-plan path. Lines that cannot be parsed come back for
    review rather than being guessed at.
    """
    state = await EDITORO.post(f"/api/projects/{project}/directives", json={"text": text})
    return {
        "blocks_placed": len(state.get("instances", [])),
        "needs_review": state.get("directives_review", []),
        "awaiting_assets": [item["id"] for item in state.get("instances", [])
                            if item.get("missing")],
    }


# -------------------------------------------------------------------- media
@mcp.tool()
async def add_asset(
    project: str,
    path: Annotated[str, Field(description="Absolute path to an image, video, audio file "
                                           "or PDF on this machine.")],
) -> dict[str, Any]:
    """Copy a local file into the project's assets folder so a template can use it."""
    source = Path(path).expanduser()
    if not source.is_file():
        raise EditoroError(f"No file at {source}")
    with open(source, "rb") as handle:
        payload = await EDITORO.post(
            f"/api/projects/{project}/asset",
            files={"file": (source.name, handle.read())},
        )
    info: dict[str, Any] = {"asset": payload["file"]}
    try:
        info.update(await EDITORO.get(f"/api/projects/{project}/asset-info",
                                      params={"asset": payload["file"]}))
    except EditoroError:
        pass
    return info


@mcp.tool()
async def import_source(
    project: str,
    path: Annotated[str, Field(description="Absolute path to the main talking-head video.")],
) -> dict[str, Any]:
    """Set the project's source footage. This resets the cuts to the whole video."""
    source = Path(path).expanduser()
    if not source.is_file():
        raise EditoroError(f"No file at {source}")
    with open(source, "rb") as handle:
        state = await EDITORO.post(
            f"/api/projects/{project}/source",
            files={"file": (source.name, handle.read())},
        )
    info = state["source_info"]
    return {
        "source": state["source"],
        "resolution": f"{info['width']}x{info['height']}",
        "fps": info["fps"], "duration": round(info["duration"], 3),
        "orientation": state["orientation"],
        "next": "generate_captions",
    }


@mcp.tool()
async def list_assets(project: str) -> dict[str, Any]:
    """List the media already imported into this project."""
    return await EDITORO.get(f"/api/projects/{project}/assets")


@mcp.tool()
async def get_asset_info(
    project: str,
    asset: Annotated[str, Field(description="File name as returned by list_assets.")],
) -> dict[str, Any]:
    """Real pixel dimensions, aspect ratio and page count for one asset.

    Call this before sizing an image or placing highlight strokes: it turns
    placement arithmetic from a guess into a calculation.
    """
    return await EDITORO.get(f"/api/projects/{project}/asset-info", params={"asset": asset})


@mcp.tool()
async def find_text_in_asset(
    project: str,
    asset: Annotated[str, Field(description="A PDF or image in the project's assets.")],
    query: Annotated[str, Field(description="Text to locate. Empty returns every line.")] = "",
    page: Annotated[int, Field(description="Zero-based page index for PDFs.", ge=0)] = 0,
) -> dict[str, Any]:
    """Locate text on a page and get highlight coordinates for it.

    For PDFs the coordinates come from the document's own text geometry, so they
    are exact - hand the returned lines straight to `highlight_lines`. Images
    have no text layer; for those, render_frame the placed block and position
    strokes from what you can see.
    """
    payload = await EDITORO.get(f"/api/projects/{project}/asset-text",
                                params={"asset": asset, "page": page, "query": query})
    if payload["source"] == "pdf" and query and not payload["lines"]:
        payload["note"] = ("Nothing matched on this page. Try a shorter phrase, or omit "
                           "`query` to see every line and pick from them.")
    return payload


@mcp.tool()
async def highlight_lines(
    project: str,
    asset: Annotated[str, Field(description="The page to show, from list_assets.")],
    quote: Annotated[str, Field(description="Verbatim spoken words that this page is "
                                            "being read out under; used to time the block.")],
    highlight: Annotated[list[str], Field(
        description="Phrases on the page to sweep the marker across, in reading order.")],
    page: Annotated[int, Field(description="Zero-based PDF page index.", ge=0)] = 0,
    duration: Annotated[float, Field(description="Seconds on screen.", ge=0.2)] = 5.0,
) -> dict[str, Any]:
    """Place a highlighted document page in one call.

    Resolves each phrase to its real position on the page, builds the marker
    strokes, anchors the block to where the quote is spoken and places it. This
    is the whole highlight workflow, and it is exact for PDFs because the
    coordinates come from the document rather than from an estimate.
    """
    located = await EDITORO.get(f"/api/projects/{project}/asset-text",
                                params={"asset": asset, "page": page})
    if located["source"] != "pdf":
        raise EditoroError(
            "This asset has no text layer, so phrases cannot be located automatically. "
            "Place a `highlight` block with place_blocks, then render_frame it and set "
            "the strokes by eye with update_blocks."
        )
    lines = located["lines"]
    strokes, unmatched = [], []
    for phrase in highlight:
        needle = phrase.strip().casefold()
        match = next((line for line in lines if needle in line["text"].casefold()), None)
        if match is None:
            tokens = [token for token in needle.split() if len(token) > 2]
            match = next((line for line in lines
                          if tokens and all(token in line["text"].casefold() for token in tokens)),
                         None)
        if match is None:
            unmatched.append(phrase)
            continue
        strokes.append({"x1": match["x1"], "x2": match["x2"],
                        "y": match["y"], "h": max(0.02, match["h"] * 1.35)})
    if not strokes:
        raise EditoroError(
            f"None of those phrases are on page {page}. Call find_text_in_asset with no "
            "query to see what the page actually says."
        )
    placed = await place_blocks(
        project=project,
        blocks=[Block(template="highlight", quote=quote, duration=duration,
                      fields={"asset": asset, "page": page, "strokes": strokes})],
    )
    if unmatched:
        placed["not_found_on_page"] = unmatched
    placed["strokes"] = strokes
    return placed


# ------------------------------------------------------------------ cutting
class Cut(BaseModel):
    """One kept span of the source video, in source seconds."""
    src_in: Annotated[float, Field(description="Where this kept span starts in the source.")]
    src_out: Annotated[float, Field(description="Where it ends in the source.")]


@mcp.tool()
async def set_cuts(
    project: str,
    keep: Annotated[list[Cut], Field(
        description="The spans of the source to keep, in order and non-overlapping. "
                    "Everything else is dropped.")],
    ripple: Annotated[bool, Field(
        description="Move placed blocks and captions to follow the new timing. Almost "
                    "always what you want; turning it off leaves them where they were.")] = True,
) -> dict[str, Any]:
    """Cut the source down to the spans worth keeping.

    Times here are **source** seconds, unlike everything else in this API - they
    describe the raw file. Removing a span shortens the timeline, so re-read the
    transcript afterwards if you plan to keep placing by timestamp; blocks
    anchored by quote survive this untouched.
    """
    state = await EDITORO.state(project)
    total = state.get("source_info", {}).get("duration", 0)
    spans = sorted(((max(0.0, cut.src_in), min(total or cut.src_out, cut.src_out))
                    for cut in keep), key=lambda pair: pair[0])
    cleaned: list[dict[str, float]] = []
    for start, end in spans:
        if end <= start:
            continue
        if cleaned and start < cleaned[-1]["src_out"]:
            raise EditoroError(
                f"Kept spans overlap around {start:.2f}s. They must be in order and disjoint.")
        cleaned.append({"src_in": round(start, 4), "src_out": round(end, 4)})
    if not cleaned:
        raise EditoroError("Keep at least one span of the source.")
    state["cuts"] = cleaned
    state["ripple"] = ripple
    state = await EDITORO.save(state)
    return {
        "cuts": state["cuts"],
        "timeline_duration": _timeline_duration(state),
        "removed_seconds": round((total or 0) - _timeline_duration(state), 3),
    }


@mcp.tool()
async def remove_range(
    project: str,
    start: Annotated[float, Field(description="Start of the span to drop, source seconds.")],
    end: Annotated[float, Field(description="End of the span to drop, source seconds.")],
) -> dict[str, Any]:
    """Drop one span of the source - a stumble, a long pause, a retake.

    Easier than restating the whole keep-list when you only want one thing gone.
    """
    state = await EDITORO.state(project)
    total = state.get("source_info", {}).get("duration", 0)
    current = state.get("cuts") or [{"src_in": 0.0, "src_out": total}]
    kept: list[Cut] = []
    for cut in current:
        if end <= cut["src_in"] or start >= cut["src_out"]:
            kept.append(Cut(src_in=cut["src_in"], src_out=cut["src_out"]))
            continue
        if start > cut["src_in"]:
            kept.append(Cut(src_in=cut["src_in"], src_out=min(start, cut["src_out"])))
        if end < cut["src_out"]:
            kept.append(Cut(src_in=max(end, cut["src_in"]), src_out=cut["src_out"]))
    return await set_cuts(project=project, keep=kept, ripple=True)


@mcp.tool()
async def set_orientation(
    project: str,
    mode: Annotated[Literal["auto", "horizontal", "vertical"], Field(
        description="auto follows the footage; the explicit values lay templates out for "
                    "16:9 or 9:16 regardless of what the source is.")],
) -> dict[str, Any]:
    """Choose which of each template's two layouts is used.

    Every template ships a 16:9 and a 9:16 variant. Placement made in one
    orientation is remembered, so switching back and forth is lossless.
    """
    state = await EDITORO.state(project)
    previous = state.get("orientation", "horizontal")
    for item in state.get("instances", []):
        item.setdefault("layouts", {})[previous] = {
            "x": item["x"], "y": item["y"], "scale": item["scale"]}
    state["orientation_mode"] = mode
    state = await EDITORO.save(state)
    active = state["orientation"]
    packs = await EDITORO.packs()
    for item in state.get("instances", []):
        saved = (item.get("layouts") or {}).get(active)
        if not saved:
            variant = packs.get(item["template"], {}).get("_variants", {}).get(active, {})
            saved = {"x": variant.get("x", 0.5), "y": variant.get("y", 0.3),
                     "scale": variant.get("scale", 1.0)}
        item.update(saved)
    state = await EDITORO.save(state)
    return {"orientation": active, "orientation_mode": mode,
            "blocks_relaid": len(state.get("instances", []))}


# ----------------------------------------------------------------- watching
@mcp.tool()
async def render_frame(
    project: str,
    at: Annotated[float, Field(description="Timeline seconds to render.", ge=0)],
    width: Annotated[int, Field(description="Output width in pixels.", ge=160, le=3840)] = 1024,
) -> ImageContent:
    """Render one finished frame - footage, camera move, templates and captions - and look at it.

    This is how you check your own work. It runs the same renderer the export
    uses, so what comes back is a real frame of the finished video, not a
    preview. Use it after placing blocks and before exporting: overlapping
    cards, text that ran long, or a card sitting over the speaker's face are all
    obvious in a still and invisible in a coordinate list.
    """
    png = await EDITORO.get(f"/api/projects/{project}/frame",
                            params={"t": at, "width": width})
    if not isinstance(png, (bytes, bytearray)):
        raise EditoroError("The renderer did not return an image.")
    return ImageContent(type="image", mimeType="image/png",
                        data=base64.b64encode(png).decode("ascii"))


@mcp.tool()
async def export(
    project: str,
    mode: Annotated[Literal["lossless", "hq"], Field(
        description="lossless stream-copies untouched footage and renders only the edited "
                    "spans - faster and visually identical to the source. hq re-encodes "
                    "everything consistently and can change resolution.")] = "lossless",
    resolution: Annotated[Literal["source", "1080", "720"], Field(
        description="Output height. Only used by hq; lossless always matches the source.")]
        = "source",
    wait: Annotated[bool, Field(description="Block until the file is written.")] = True,
) -> dict[str, Any]:
    """Render the finished video to an MP4 in the project's exports folder."""
    await EDITORO.post(f"/api/projects/{project}/export",
                       json={"mode": mode, "resolution": resolution})
    if not wait:
        return {"started": True, "note": "Poll with export_status."}
    deadline = time.monotonic() + 7200
    last_seen = {file["name"] for file in
                 (await EDITORO.get(f"/api/projects/{project}/exports"))["files"]}
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        listing = await EDITORO.get(f"/api/projects/{project}/exports")
        fresh = [file for file in listing["files"] if file["name"] not in last_seen]
        if fresh:
            newest = fresh[0]
            return {
                "exported": newest["name"],
                "size_mb": round(newest["size"] / 1048576, 2),
                "path": str(ROOT / "projects" / project / "exports" / newest["name"]),
                "url": f"{EDITORO.base_url}{newest['url']}",
            }
        log = await _export_log_tail(project)
        if "EXPORT FAILED" in log:
            raise EditoroError("Export failed. Tail of export.log:\n"
                               + log[-1200:])
        if "EXPORT CANCELED" in log:
            return {"canceled": True}
    raise EditoroError("Export did not finish within two hours.")


async def _export_log_tail(project: str) -> str:
    log = ROOT / "projects" / project / "exports" / "export.log"
    if not log.is_file():
        return ""
    return log.read_text(encoding="utf-8", errors="replace")


@mcp.tool()
async def export_status(project: str) -> dict[str, Any]:
    """Completed exports for a project, newest first, plus any recent failure."""
    listing = await EDITORO.get(f"/api/projects/{project}/exports")
    log = await _export_log_tail(project)
    status: dict[str, Any] = {
        "files": [{"name": file["name"], "size_mb": round(file["size"] / 1048576, 2)}
                  for file in listing["files"][:8]],
    }
    if "EXPORT FAILED" in log:
        status["last_error"] = log.split("EXPORT FAILED:")[-1].strip()[:600]
    return status


@mcp.tool()
async def set_notes(
    project: str,
    notes: Annotated[str, Field(description="Free text. Replaces whatever is stored.")],
) -> dict[str, Any]:
    """Leave a note on the project - an editing plan, open questions, what you changed.

    Persisted with the project, so it survives between sessions and is visible to
    whoever opens the editor next.
    """
    state = await EDITORO.state(project)
    state["notes"] = notes[:8000]
    await EDITORO.save(state)
    return {"saved": True, "characters": len(notes[:8000])}


# ---------------------------------------------------------------- resources
@mcp.resource("editoro://templates", mime_type="application/json",
              description="The full template catalogue with fields and layouts.")
async def resource_templates() -> str:
    packs = await EDITORO.packs()
    return json.dumps(
        [_describe_pack(pack, full=True) for pack in packs.values()],
        ensure_ascii=False, indent=2,
    )


@mcp.resource("editoro://projects", mime_type="application/json",
              description="Every project with its footage and length.")
async def resource_projects() -> str:
    payload = await EDITORO.get("/api/projects")
    return json.dumps(payload["projects"], ensure_ascii=False, indent=2)


@mcp.resource("editoro://projects/{project}/transcript", mime_type="text/plain",
              description="The timed transcript of one project.")
async def resource_transcript(project: str) -> str:
    payload = await EDITORO.get(f"/api/projects/{project}/transcript")
    return "\n".join(
        f"[{block['start']:7.2f} - {block['end']:7.2f}] {block['text']}"
        for block in payload["blocks"]
    ) or "(no transcript yet)"


@mcp.resource("editoro://projects/{project}/timeline", mime_type="application/json",
              description="Everything currently placed on one project's timeline.")
async def resource_timeline(project: str) -> str:
    state = await EDITORO.state(project)
    return json.dumps({
        "orientation": state.get("orientation"),
        "duration": _timeline_duration(state),
        "cuts": state.get("cuts", []),
        "instances": [_brief_instance(item) for item in state.get("instances", [])],
    }, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ prompts
@mcp.prompt(
    name="edit_video",
    title="Edit a video end to end",
    description="A working order for turning raw talking-head footage into a finished edit.",
)
def prompt_edit_video(
    project: Annotated[str, Field(description="Project name.")],
    brief: Annotated[str, Field(description="What the video is about and who it is for.")] = "",
) -> str:
    return f"""Edit the Editoro project "{project}" end to end.

{f'Brief: {brief}' if brief else ''}

Work in this order:

1. `get_project` and `diagnostics`. Confirm there is source footage and that no
   template pack failed to load.
2. `get_transcript`. Read the whole thing before placing anything. Note where
   the speaker names a number, a term, a source, a list, or a comparison - those
   are where a template earns its place.
3. `list_templates` once. Read the field descriptions; they are the contract.
4. Cut first if the footage needs it: `remove_range` for stumbles and dead air.
   Cutting after placing means re-checking every timestamp.
5. `place_blocks` in one call for the whole edit. Anchor with `quote`, not `at`.
   Keyword and quote text must be verbatim - the words that were actually said.
   One camera move at a time. Leave gaps; not every sentence needs a graphic.
6. `render_frame` at three or four moments, including one where two blocks are
   close together. Look at the frames. Fix what is wrong with `update_blocks`.
7. `set_caption_style` if the captions sit badly over anything you placed.
8. `export` with mode "lossless".

If something is ambiguous - which of two readings a quote refers to, whether a
claim needs a source card - ask rather than guessing. If a block needs media
that does not exist yet, place it anyway; it will be flagged as awaiting an
asset, and that list is easier to act on than a gap.
"""


# --------------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Editoro MCP server",
        epilog="Set EDITORO_URL to point at an Editoro running somewhere else, "
               "or EDITORO_AUTOSTART=0 to never start one.",
    )
    parser.add_argument(
        "--transport", default="stdio",
        choices=["stdio", "streamable-http", "sse"],
        help="stdio for desktop MCP clients; streamable-http or sse to serve over a port.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for HTTP transports.")
    parser.add_argument("--port", type=int, default=8766, help="Port for HTTP transports.")
    parser.add_argument("--print-config", action="store_true",
                        help="Print a ready-to-paste MCP client config block and exit.")
    args = parser.parse_args()

    if args.print_config:
        print(json.dumps({
            "mcpServers": {
                "editoro": {
                    "command": str(_python_for_editoro()),
                    "args": [str(ROOT / "mcp_server.py")],
                }
            }
        }, indent=2))
        return 0

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        print(f"[editoro-mcp] {args.transport} on http://{args.host}:{args.port}/mcp",
              file=sys.stderr)
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        print("[editoro-mcp] warning: ffmpeg is not on PATH; export will fail.",
              file=sys.stderr)
    raise SystemExit(main())
