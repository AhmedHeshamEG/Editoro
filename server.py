# ============================================================================
#  EDITORO — server.py
#  Single-file backend: FastAPI + uvicorn + ffmpeg/ffprobe subprocesses.
#  Sections:
#    [1] Imports & config
#    [2] Pydantic models (project state)
#    [3] Template pack scanning
#    [4] Template foley event resolution
#    [5] Media helpers (ffprobe, waveform peaks, thumbnails, PDF raster)
#    [6] Directive parser (da7ee7-director grammar)  + unit tests
#    [7] HTTP API (projects, uploads, media, directives)
#    [8] Range-request file serving (mandatory for iOS Safari scrubbing)
#    [9] WebSocket state sync + export progress
#   [10] Export pipeline (smart rendering: stream-copy + NVENC re-encode)
#   [11] Entrypoint
# ============================================================================

# ---------------------------------------------------------------- [1] Imports
from __future__ import annotations
import copy
import unicodedata
import asyncio, base64, collections, hashlib, json, math, mimetypes, os, re, shutil, struct, subprocess, sys, tempfile, threading, time, uuid, webbrowser
import importlib.util
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
import uvicorn

ROOT = Path(__file__).parent.resolve()
TEMPLATES_DIR = ROOT / "templates"
PROJECTS_DIR = ROOT / "projects"
PROJECTS_DIR.mkdir(exist_ok=True)

PORT = int(os.environ.get("EDITORO_PORT", "8765"))
APP_VERSION = "2.0.0"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
PRIVATE_BROWSERS_DIR = ROOT / ".venv" / "playwright-browsers"
WHISPER_MODELS_DIR = ROOT / ".models" / "whisper"
if PRIVATE_BROWSERS_DIR.is_dir():
    # `launch.cmd` sets this too, but keeping the server self-contained makes
    # direct starts, tests, and export workers use the same private renderer.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PRIVATE_BROWSERS_DIR))
SAFE_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_FILE = re.compile(r"[^A-Za-z0-9._ -]+")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
ASSET_EXTENSIONS = VIDEO_EXTENSIONS | {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".wav", ".mp3", ".m4a"}


# On Windows a child spawned from a console joins that console's process
# group, so a Ctrl-C or Ctrl-Break delivered to Editoro's window is delivered
# to every FFmpeg it started as well. An export that dies that way exits with
# 0xC000013A (3221225786) part-way through writing a span, and the join step
# then has nothing to assemble. CREATE_NEW_PROCESS_GROUP takes the children out
# of that group; CREATE_NO_WINDOW stops each one flashing a console of its own.
# Cancellation still works, because cancellation terminates the process handle
# directly rather than relying on a console event.
CHILD_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    if sys.platform == "win32" else 0
)


def _append_log(log_file: Optional[Path], line: str) -> None:
    """One note in the export log, for things that are not a subprocess."""
    if not log_file:
        return
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def run(cmd: list[str], log_file: Optional[Path] = None, check: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess, optionally appending full command + output to a log."""
    p = subprocess.run(cmd, capture_output=True, text=True, creationflags=CHILD_FLAGS)
    if log_file:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write("\n$ " + " ".join(cmd) + "\n" + (p.stderr or "") + "\n")
    if check and p.returncode:
        detail = (p.stderr or p.stdout or "command failed").strip()[-2000:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({p.returncode}): {detail}")
    return p


async def run_cancelable(
    cmd: list[str],
    log_file: Optional[Path],
    cancel: asyncio.Event,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run an export subprocess while honoring cancellation immediately."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=CHILD_FLAGS,
    )
    communicate = asyncio.create_task(process.communicate())
    canceled = asyncio.create_task(cancel.wait())
    done, _ = await asyncio.wait(
        {communicate, canceled},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if canceled in done and cancel.is_set() and not communicate.done():
        process.terminate()
        try:
            await asyncio.wait_for(communicate, timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await communicate
        raise asyncio.CancelledError()
    canceled.cancel()
    await asyncio.gather(canceled, return_exceptions=True)
    stdout_bytes, stderr_bytes = await communicate
    stdout = stdout_bytes.decode("utf-8", "replace")
    stderr = stderr_bytes.decode("utf-8", "replace")
    result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if log_file:
        with open(log_file, "a", encoding="utf-8") as handle:
            handle.write("\n$ " + " ".join(cmd) + "\n" + stderr + "\n")
    if check and result.returncode:
        detail = (stderr or stdout or "command failed").strip()[-2000:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({result.returncode}): {detail}")
    return result


# ------------------------------------------------------- [2] Pydantic models
class MediaInfo(BaseModel):
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 30.0
    duration: float = 0.0
    audio_codec: str = ""
    # Matched by the encoder so re-encoded spans join copied spans invisibly.
    profile: str = ""
    pix_fmt: str = ""
    bit_rate: int = 0


class Placement(BaseModel):
    """Where an instance sits, for one orientation."""
    x: float = Field(default=0.5, ge=-1, le=2)
    y: float = Field(default=0.3, ge=-1, le=2)
    scale: float = Field(default=1.0, ge=0.05, le=10)


class Instance(BaseModel):
    """One placed template instance on an overlay track."""
    id: str
    template: str                     # template pack id
    track: int = Field(default=1, ge=1, le=64)
    start: float = Field(ge=0)
    duration: float = Field(gt=0, le=86400)
    x: float = Field(default=0.5, ge=-1, le=2)
    y: float = Field(default=0.3, ge=-1, le=2)
    scale: float = Field(default=1.0, ge=0.05, le=10)
    # Every template ships a 16:9 and a 9:16 layout. `x`/`y`/`scale` are the
    # active placement for the project's current orientation; `layouts` keeps
    # the other one, so switching orientation and switching back is lossless
    # instead of resetting everything the editor moved by hand.
    layouts: dict[Literal["horizontal", "vertical"], Placement] = Field(default_factory=dict)
    fields: dict[str, Any] = Field(default_factory=dict)  # text, asset, strokes, rect, volume...
    missing: bool = False             # asset not yet filled
    review: bool = False              # ⚠️ (meme directives)
    locked: bool = False              # ignored by ripple edits and agent bulk ops
    # Frosted glass. When true, everything composited below this block - the
    # footage and any lower-track block - is blurred, and this block and
    # everything above it stay sharp. It is a property of the block rather than
    # a separate timeline object so that moving the block moves its blur, and
    # so the blur can never be left behind on the timeline by accident.
    backdrop: bool = False
    # Where this block sits relative to the speaker. "front" is over them, the
    # way every overlay has always worked. "behind" composites it between the
    # background and the speaker, using the same matte the look already builds,
    # so the block passes behind their head instead of across it. This is a
    # toggle rather than a track order because track order means time-and-stack
    # everywhere else in the editor, and overloading it here would make two
    # unrelated things share one control.
    depth: Literal["front", "behind"] = "front"
    # Foley, per block. A template's sounds are generated from its pack, which
    # is right nearly always and wrong when three blocks land in four seconds
    # and the edit starts to tick. Silencing is a property of the block for the
    # same reason `backdrop` is: the sound belongs to the thing that made it, so
    # moving the block moves its foley and deleting the block takes the sound
    # with it. A silenced block contributes no events to the mix AND no markers
    # to the SFX track, so that track always shows exactly what will be heard.
    #
    # Three states rather than two, because "silent or not" was the wrong
    # question for a block that makes more than one kind of sound. A stat card
    # opens with a pop and then ticks while the number climbs; the pop is a
    # different decision from the ticking, and one switch meant the only way to
    # lose the pop was to lose the count with it.
    #   off  - nothing at all
    #   lite - the body of the block, without whatever opens it
    #   full - everything the pack declares
    # `lite` is the default because an opening hit is the sound an edit ends up
    # with too many of: every block that has one fires it in its first frame.
    foley: Literal["off", "lite", "full"] = "lite"

    @model_validator(mode="before")
    @classmethod
    def _legacy_silent(cls, data: Any) -> Any:
        """Projects written before foley had three states carry `silent`."""
        if isinstance(data, dict) and "foley" not in data and "silent" in data:
            data = {**data, "foley": "off" if data.get("silent") else "lite"}
        return data


class CaptionWord(BaseModel):
    w: str
    s: float
    e: float
    p: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.s < 0 or self.e < self.s:
            raise ValueError("caption word timestamps are invalid")
        return self


class CaptionBlock(BaseModel):
    id: str
    start: float
    end: float
    text: str
    words: list[CaptionWord] = Field(default_factory=list)
    # Captions follow the project-wide placement unless this one was dragged
    # somewhere specific; then it keeps its own spot and stops following.
    follow_global: bool = True
    x: Optional[float] = Field(default=None, ge=0, le=1)
    y: Optional[float] = Field(default=None, ge=0, le=1)
    scale: Optional[float] = Field(default=None, ge=0.2, le=4)
    # The project-wide caption placement is kept per orientation, so a pinned
    # caption has to be too. Without this, dragging one caption in 16:9 moves
    # it in 9:16 as well, which is exactly the thing the rest of the editor
    # goes out of its way to avoid.
    layouts: dict[Literal["horizontal", "vertical"], Placement] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ordered(self):
        if self.start < 0 or self.end <= self.start:
            raise ValueError("caption timestamps are invalid")
        return self


class CaptionLayout(BaseModel):
    """Caption placement for one orientation."""
    x: float = Field(default=0.5, ge=0, le=1)
    y: float = Field(default=0.86, ge=0, le=1)
    scale: float = Field(default=1.0, ge=0.2, le=4)
    align: Literal["center", "left", "right"] = "center"
    max_width: float = Field(default=0.84, gt=0.1, le=1.0)


class CaptionStyle(BaseModel):
    """Project-wide caption behaviour, resolved per orientation."""
    horizontal: CaptionLayout = Field(default_factory=lambda: CaptionLayout(y=0.86, max_width=0.84))
    vertical: CaptionLayout = Field(default_factory=lambda: CaptionLayout(y=0.78, max_width=0.90))
    mode: Literal["chunk", "sentence"] = "chunk"
    words_per_chunk: int = Field(default=5, ge=1, le=24)
    highlight_active_word: bool = True
    box: bool = True
    font_size: float = Field(default=44, ge=12, le=160)
    preset: Literal["lower", "center", "upper", "custom"] = "lower"


class LookRequest(BaseModel):
    """What the panel and the agent are allowed to change about the look."""
    defocus: Optional[float] = Field(default=None, ge=0, le=1)
    grade: Optional[float] = Field(default=None, ge=0, le=1)


class Look(BaseModel):
    """Project-level defocus and grade. Two numbers; everything else derived."""
    # Amount of background defocus, 0 = off. The blur radius is a fraction of
    # the frame's short edge, so one number means the same look whether the
    # footage is 1080p landscape or 4K vertical.
    defocus: float = Field(default=0.0, ge=0, le=1)
    # Strength of the automatic grade, 0 = off. The LUT is written already
    # interpolated toward identity by this amount, so the preview and the
    # export read one table instead of running two implementations of the same
    # intent and hoping they agree.
    grade: float = Field(default=0.0, ge=0, le=1)
    # The source revision each artefact was built from. Replacing the footage
    # changes that revision, which is what makes a stale matte stop being used
    # rather than being quietly composited over different pixels.
    matte_revision: str = ""
    grade_revision: str = ""
    analysis: dict[str, Any] = Field(default_factory=dict)


class Preview(BaseModel):
    """Which proxy files exist and what they were built from.

    The editor plays a proxy rather than the source. A talking-head master is
    routinely 4K and several gigabytes, and no browser scrubs that smoothly -
    but a 960-pixel copy with a short GOP scrubs instantly, and the export
    never reads it, so nothing about the finished video depends on it.
    """
    # source_revision the plain proxy was built from.
    base_key: str = ""
    # source_revision plus the look amounts the baked-look proxy was built
    # from. When this matches, the browser plays a proxy that already has the
    # look in it and skips the live compositor entirely.
    look_key: str = ""


class Cut(BaseModel):
    """Kept segment of the source video, in source-time coordinates, ordered."""
    src_in: float
    src_out: float

    @model_validator(mode="after")
    def ordered(self):
        if self.src_in < 0 or self.src_out <= self.src_in:
            raise ValueError("cut range is invalid")
        return self


class SpeakerRegion(BaseModel):
    """Where the speaker is in the source frame, as fractions of it.

    Set once for the project, the way the caption position is: a talking head
    does not move between shots, so asking for it per block would be asking the
    same question over and over. A PiP block can still override it when one
    shot is framed differently, and does that through the same drag-a-box tool
    on the preview.

    The default is a tall box through the middle of the frame, which is where a
    person sitting in front of a camera actually is - not the whole frame, which
    is what the old PiP effectively used and why it showed a corner of a
    shoulder instead of a face.
    """
    x: float = Field(default=0.30, ge=0, le=1)
    y: float = Field(default=0.04, ge=0, le=1)
    w: float = Field(default=0.40, gt=0.02, le=1)
    h: float = Field(default=0.84, gt=0.02, le=1)


class ThumbElement(BaseModel):
    """One thing on the thumbnail canvas.

    Deliberately a flat list of five kinds rather than the template packs. A
    template is a thing that animates over footage for a few seconds; a
    thumbnail element is a thing that sits still forever and has to survive
    being three centimetres wide in a feed. They want different type weights,
    different outlines and no motion at all, so sharing the pack machinery
    would mean every pack growing a second personality.
    """
    id: str
    kind: Literal["headline", "subhead", "cutout", "image", "marker"]
    text: str = Field(default="", max_length=120)
    asset: str = ""                   # filename in assets/, for kind="image"
    marker: Literal["arrow", "circle", "cross", "underline"] = "arrow"
    color: str = Field(default="", max_length=9)   # "" = layout's tuned colour
    x: float = Field(default=0.5, ge=-1, le=2)
    y: float = Field(default=0.5, ge=-1, le=2)
    scale: float = Field(default=1.0, ge=0.05, le=10)
    rotate: float = Field(default=0.0, ge=-45, le=45)
    # The same trick the timeline blocks use: the inactive orientation's
    # placement is parked here, so designing once and exporting both sizes is
    # lossless rather than a reset.
    layouts: dict[Literal["horizontal", "vertical"], Placement] = Field(default_factory=dict)


class Thumbnail(BaseModel):
    """The first-frame / cover design for this project.

    A preset chooses the starting arrangement and then gets out of the way -
    every element stays movable, scalable and deletable, and more can be added.
    The preset exists to answer "where does the text go" in one click, not to
    lock the design.
    """
    layout: str = "headline-left"
    # Where the picture behind it comes from. "frame" grabs `frame_time` off
    # the timeline; "asset" uses a dropped file; "color" is a flat ground for
    # when the cutout is the whole picture.
    base: Literal["frame", "asset", "color"] = "frame"
    frame_time: float = Field(default=0.0, ge=0)
    asset: str = ""
    background: str = Field(default="#141210", max_length=9)
    # Cut the speaker out of the base frame and composite them back on top of
    # everything, so headline text can pass behind their shoulder. Needs the
    # subject matte, same as depth="behind" does.
    cutout: bool = True
    elements: list[ThumbElement] = Field(default_factory=list)
    # Seconds of the finished design held on the front of the exported video.
    # 0 means the design is only ever written out as an image file.
    hold: float = Field(default=0.0, ge=0, le=5)


class ProjectState(BaseModel):
    version: int = 1
    name: str
    source: str = ""                  # filename of source video inside project folder
    source_revision: str = ""         # cache-buster changed on every source replacement
    source_info: MediaInfo = Field(default_factory=MediaInfo)
    cuts: list[Cut] = Field(default_factory=list)          # empty = whole source
    instances: list[Instance] = Field(default_factory=list)
    captions: list[CaptionBlock] = Field(default_factory=list)
    orientation: Literal["horizontal", "vertical"] = "horizontal"
    # "auto" follows the source footage; the explicit values let a vertical cut
    # be laid out from horizontal footage (or the reverse) before reframing.
    orientation_mode: Literal["auto", "horizontal", "vertical"] = "auto"
    caption_style: CaptionStyle = Field(default_factory=CaptionStyle)
    directives_review: list[str] = Field(default_factory=list)  # unparseable lines
    ripple: bool = True
    notes: str = Field(default="", max_length=8000)   # free text for agents and humans
    look: Look = Field(default_factory=Look)  # defocus + grade, per project
    # Where the speaker stands. Read by any template that shows the footage
    # inside itself, which today means the PiP window.
    speaker_region: SpeakerRegion = Field(default_factory=SpeakerRegion)
    preview: Preview = Field(default_factory=Preview)   # proxy files, editor only
    # The frame is never completely still. A very slow scale oscillation runs
    # under the whole project on one clock, so the footage and every overlay
    # breathe in phase instead of each drifting on its own random seed. It is a
    # project setting rather than a block because its whole value is that it is
    # everywhere and unbroken; a `breathe` block overrides the strength over a
    # stretch, and a hand-placed camera move switches it off for its own span so
    # two moves never stack. See BREATHE_LEVELS for the tuned numbers.
    breathe: Literal["off", "subtle", "standard", "strong"] = "standard"
    thumbnail: Thumbnail = Field(default_factory=Thumbnail)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not SAFE_PROJECT.fullmatch(value):
            raise ValueError("project names may contain letters, numbers, underscores, and hyphens")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.orientation_mode == "auto":
            width, height = self.source_info.width, self.source_info.height
            if width and height:
                self.orientation = "vertical" if height > width else "horizontal"
        else:
            self.orientation = self.orientation_mode
        fps = max(1.0, self.source_info.fps or 30)
        frame = 1 / fps

        def snap(value: float) -> float:
            return math.floor(max(0.0, value) * fps + 0.5) / fps

        for cut in self.cuts:
            cut.src_in = snap(cut.src_in)
            cut.src_out = (
                self.source_info.duration
                if self.source_info.duration and abs(cut.src_out - self.source_info.duration) <= frame
                else snap(cut.src_out)
            )
        self.cuts.sort(key=lambda c: c.src_in)
        previous_out = -1.0
        if self.source_info.duration:
            for cut in self.cuts:
                if cut.src_out > self.source_info.duration + 0.05:
                    raise ValueError("cut exceeds source duration")
                if cut.src_in < previous_out - 1e-6:
                    raise ValueError("source cuts may not overlap")
                previous_out = cut.src_out
        elif self.cuts:
            raise ValueError("source cuts require readable source metadata")
        instance_ids = [item.id for item in self.instances]
        caption_ids = [item.id for item in self.captions]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("template instance ids must be unique")
        if len(caption_ids) != len(set(caption_ids)):
            raise ValueError("caption ids must be unique")
        timeline_duration = sum(cut.src_out - cut.src_in for cut in self.cuts)
        if not timeline_duration:
            timeline_duration = self.source_info.duration
        for item in self.instances:
            # Seed the active layout for states written before layouts existed,
            # but never overwrite one that is already stored: switching
            # orientation writes the incoming layout and then saves, and
            # clobbering it here would make every switch reset the placement it
            # was supposed to restore.
            item.layouts.setdefault(
                self.orientation, Placement(x=item.x, y=item.y, scale=item.scale))
            item.start = snap(item.start)
            item.duration = max(frame, snap(item.duration))
            if timeline_duration:
                item.start = min(item.start, max(0.0, timeline_duration - frame))
                item.duration = min(item.duration, max(frame, timeline_duration - item.start))
        for block in self.captions:
            block.start = snap(block.start)
            block.end = max(block.start + frame, snap(block.end))
            if timeline_duration:
                block.start = min(block.start, max(0.0, timeline_duration - frame))
                block.end = min(timeline_duration, max(block.start + frame, block.end))
            # Same rule as instances: seed the active orientation from the flat
            # fields for states written before captions had layouts, but never
            # overwrite one that is already stored.
            if not block.follow_global and block.x is not None and block.y is not None:
                block.layouts.setdefault(self.orientation, Placement(
                    x=block.x, y=block.y, scale=block.scale or 1.0))
        self.instances.sort(key=lambda item: (item.start, item.track, item.id))
        self.captions.sort(key=lambda item: (item.start, item.id))
        return self


class TranscriptionRequest(BaseModel):
    model: Literal["large-v3", "large-v3-turbo"] = "large-v3"
    language: Literal["auto", "en", "ar"] = "auto"
    terms: str = Field(default="", max_length=2000)


def project_dir(name: str) -> Path:
    if not SAFE_PROJECT.fullmatch(name):
        raise ValueError("invalid project name")
    p = (PROJECTS_DIR / name).resolve()
    if PROJECTS_DIR not in p.parents:
        raise ValueError("bad project name")
    return p


def require_project(name: str) -> Path:
    """Resolve an existing project or return a useful client-facing error."""
    try:
        folder = project_dir(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not (folder / "project.json").is_file():
        raise HTTPException(404, "Project not found")
    return folder


def load_state(name: str) -> ProjectState:
    f = project_dir(name) / "project.json"
    if f.exists():
        try:
            return ProjectState.model_validate_json(f.read_text(encoding="utf-8"))
        except Exception as exc:
            backup = f.with_suffix(".json.broken")
            shutil.copy2(f, backup)
            raise RuntimeError(f"Project state is invalid; backup written to {backup.name}: {exc}") from exc
    return ProjectState(name=name)


def save_state(st: ProjectState) -> ProjectState:
    st = ProjectState.model_validate(st.model_dump())
    d = project_dir(st.name)
    d.mkdir(parents=True, exist_ok=True)
    target = d / "project.json"
    temporary = d / ".project.json.tmp"
    temporary.write_text(st.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return st


def safe_upload_name(filename: Optional[str], allowed: set[str]) -> str:
    raw = Path(filename or "upload").name.strip()
    cleaned = SAFE_FILE.sub("-", raw).strip(" .")
    if not cleaned or Path(cleaned).suffix.lower() not in allowed:
        raise HTTPException(415, f"Unsupported file type: {Path(raw).suffix or 'none'}")
    return cleaned[:160]


def unique_path(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    stem, suffix, counter = candidate.stem, candidate.suffix, 2
    while candidate.exists():
        candidate = folder / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


# ---------------------------------------------- [3] Template pack scanning
TEMPLATE_PACKS: dict[str, dict] = {}
VERB_TO_TEMPLATE: dict[str, tuple[str, bool]] = {}  # verb -> (template_id, review_flag)
TEMPLATE_ERRORS: dict[str, str] = {}
ORIENTATIONS = ("horizontal", "vertical")


FIELD_TYPES = {
    "text", "textarea", "asset:image", "asset:video", "strokes", "rect",
    "rect2", "number", "select", "boolean", "color", "none",
}
CAMERA_MODES = {"hold", "reveal", "drift", "impact", "whip"}
SHARED_DIR_NAME = "_shared"


def sfx_file(spec: dict[str, Any], name: str) -> Path:
    """Resolve one pack sound: its own file, or one from the shared library."""
    if name.startswith(SHARED_DIR_NAME + "/"):
        return TEMPLATES_DIR / name
    return TEMPLATES_DIR / spec.get("_dir", spec.get("id", "")) / name


def shared_assets() -> list[str]:
    """Every file under templates/_shared that a renderer may need up front."""
    root = TEMPLATES_DIR / SHARED_DIR_NAME
    if not root.is_dir():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".webp", ".woff2"}
    )


def _check_motion(motion: dict[str, Any]) -> None:
    """Validate the declarative motion block so a bad pack fails at scan time."""
    for key in ("in", "out"):
        phase = motion.get(key)
        if phase is None:
            continue
        if not isinstance(phase, dict):
            raise ValueError(f"motion.{key} must be an object")
        kind = phase.get("type", "ease")
        if kind not in {"ease", "spring"}:
            raise ValueError(f"motion.{key}.type must be 'ease' or 'spring'")
        if kind == "spring":
            for number in ("stiffness", "damping"):
                if number in phase and not isinstance(phase[number], (int, float)):
                    raise ValueError(f"motion.{key}.{number} must be numeric")
        if "ms" in phase and not 0 <= float(phase["ms"]) <= 8000:
            raise ValueError(f"motion.{key}.ms must be between 0 and 8000")
        transform = phase.get("from" if key == "in" else "to", {})
        if not isinstance(transform, dict):
            raise ValueError(f"motion.{key} transform must be an object")
        unknown = set(transform) - {"opacity", "scale", "x", "y", "rotate"}
        if unknown:
            raise ValueError(
                f"motion.{key} has unknown transform keys: {', '.join(sorted(unknown))}"
            )
    stagger = motion.get("stagger")
    if stagger is not None:
        if not isinstance(stagger.get("elements", []), list):
            raise ValueError("motion.stagger.elements must be a list of element names")
        if not isinstance(stagger.get("step_ms", 0), (int, float)):
            raise ValueError("motion.stagger.step_ms must be numeric")
    shadow = motion.get("shadow")
    if shadow is not None and not 1 <= int(shadow.get("layers", 3)) <= 6:
        raise ValueError("motion.shadow.layers must be between 1 and 6")
    blur = motion.get("blur")
    if blur is not None and not 1 <= int(blur.get("max", 6)) <= 12:
        raise ValueError("motion.blur.max must be between 1 and 12")
    value = motion.get("value")
    if value is not None:
        if not isinstance(value, dict):
            raise ValueError("motion.value must be an object")
        steps = value.get("steps", 0)
        if not isinstance(steps, (int, float)) or not 0 <= int(steps) <= 64:
            raise ValueError("motion.value.steps must be between 0 and 64")
        ratio = value.get("ratio", 1)
        if not isinstance(ratio, (int, float)) or not 1 <= float(ratio) <= 2:
            raise ValueError("motion.value.ratio must be between 1 and 2")
        keys = value.get("steps_from")
        if keys is not None:
            if not isinstance(keys, dict):
                raise ValueError("motion.value.steps_from must be an object")
            unknown = set(keys) - {"value", "start", "step"}
            if unknown:
                raise ValueError(
                    "motion.value.steps_from has unknown keys: " + ", ".join(sorted(unknown))
                )
            if not keys.get("step"):
                raise ValueError("motion.value.steps_from must name a 'step' field")


def _check_variants(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every template ships a 16:9 and a 9:16 layout; neither is optional."""
    variants = spec.get("variants")
    if variants is None:
        zones = spec.get("zones") or {}
        variants = {orientation: dict(zones.get(orientation, {})) for orientation in ORIENTATIONS}
    if not isinstance(variants, dict):
        raise ValueError("variants must be an object")
    base = variants.get("base", {})
    if not isinstance(base, dict):
        raise ValueError("variants.base must be an object")
    resolved: dict[str, dict[str, Any]] = {"base": base}
    for orientation in ORIENTATIONS:
        variant = variants.get(orientation)
        if not isinstance(variant, dict):
            raise ValueError(f"variants.{orientation} is required (16:9 and 9:16 both ship)")
        merged = {**base, **variant}
        for key in ("x", "y", "scale"):
            if not isinstance(merged.get(key), (int, float)):
                raise ValueError(f"variants.{orientation}.{key} must be numeric")
        if not (0 <= float(merged["x"]) <= 1 and 0 <= float(merged["y"]) <= 1):
            raise ValueError(f"variants.{orientation} x/y must be inside the frame")
        resolved[orientation] = merged
    return resolved


def scan_templates() -> None:
    TEMPLATE_PACKS.clear()
    VERB_TO_TEMPLATE.clear()
    TEMPLATE_ERRORS.clear()
    colors: dict[str, str] = {}
    for d in sorted(TEMPLATES_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        tj = d / "template.json"
        if not tj.exists():
            continue
        try:
            spec = json.loads(tj.read_text(encoding="utf-8"))
            required = {"id", "display_name", "category_color", "fields", "default_duration"}
            missing = required - spec.keys()
            if missing:
                raise ValueError(f"missing keys: {', '.join(sorted(missing))}")
            if spec["id"] != d.name or not re.fullmatch(r"[a-z0-9-]+", spec["id"]):
                raise ValueError("template id must match its folder")
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}", spec["category_color"]):
                raise ValueError("category_color must be #RRGGBB")
            color = spec["category_color"].upper()
            if color in colors:
                raise ValueError(f"category_color is already used by {colors[color]}")
            colors[color] = spec["id"]
            if not isinstance(spec["fields"], list):
                raise ValueError("fields must be a list")
            names: set[str] = set()
            for field in spec["fields"]:
                if field.get("type") not in FIELD_TYPES:
                    raise ValueError(f"unsupported field type: {field.get('type')}")
                name = field.get("name")
                if not isinstance(name, str) or not name or name in names:
                    raise ValueError("field names must be non-empty and unique")
                if field["type"] == "select" and not isinstance(field.get("options"), list):
                    raise ValueError(f"field {name}: select needs an options list")
                names.add(name)
            spec["_dir"] = d.name
            spec["_variants"] = _check_variants(spec)
            spec.setdefault("zones", {
                orientation: {
                    key: spec["_variants"][orientation][key] for key in ("x", "y", "scale")
                }
                for orientation in ORIENTATIONS
            })
            if "motion" in spec:
                _check_motion(spec["motion"])
                # A field name is only a name until something has to read it:
                # a typo here would silently fall back to the pack's own step
                # count and the count would quietly stop honouring the block.
                for role, field_name in (
                        ((spec["motion"].get("value") or {}).get("steps_from") or {}).items()):
                    if field_name not in names:
                        raise ValueError(
                            f"motion.value.steps_from.{role} names no field: {field_name}"
                        )
            camera = spec.get("camera")
            if camera is not None and camera.get("mode") not in CAMERA_MODES:
                raise ValueError(
                    f"camera.mode must be one of {', '.join(sorted(CAMERA_MODES))}"
                )
            duration = float(spec["default_duration"])
            if duration <= 0 and spec["id"] != "captions":
                raise ValueError("default_duration must be positive")
            for sfx in spec.get("sfx", []):
                if not sfx_file(spec, sfx["file"]).is_file():
                    raise ValueError(f"missing SFX: {sfx['file']}")
                if not 0 <= float(sfx.get("gain", 0.5)) <= 1.5:
                    raise ValueError(f"sfx gain out of range: {sfx['file']}")
                if "lead" in sfx and not isinstance(sfx["lead"], bool):
                    raise ValueError(f"sfx lead must be true or false: {sfx['file']}")
                if "follow_value" in sfx:
                    # A sound can only follow a number that actually steps;
                    # against a smooth ramp there is nothing to tick on.
                    motion_block = spec.get("motion") or {}
                    if not int((motion_block.get("value") or {}).get("steps", 0)):
                        raise ValueError(
                            f"{sfx['file']} follows a value, so motion.value.steps is required"
                        )
                    named = str(sfx["follow_value"] or "")
                    known = (motion_block.get("stagger") or {}).get("elements") or []
                    if named and named not in known:
                        raise ValueError(
                            f"follow_value '{named}' is not one of motion.stagger.elements"
                        )
            sounds = spec.get("sfx", [])
            if sounds and all(s.get("lead") for s in sounds):
                # `lite` is the default, so a pack whose every sound is a lead
                # would place silently and read as broken rather than as quiet.
                raise ValueError("every sfx entry is a lead: the block would be silent by default")
            for asset in spec.get("assets", []):
                if not (d / asset).is_file():
                    raise ValueError(f"missing asset: {asset}")
            spec["_has_render"] = (d / "render.js").exists()
            if not spec["_has_render"]:
                raise ValueError("render.js is required; the engine has no built-in renderers")
            # The UI imports render.js as an ES module, and the browser caches
            # module URLs hard - it will not even revalidate one it already has.
            # A hand-written "version" in template.json only busts that cache if
            # somebody remembers to bump it, and forgetting is silent: the edited
            # file sits on disk while the page keeps running the old one. So the
            # cache key is the file's own mtime, which cannot be forgotten.
            spec["_rev"] = str(int((d / "render.js").stat().st_mtime))
            TEMPLATE_PACKS[spec["id"]] = spec
            review_flag = bool(spec.get("directive_review"))
            verb = spec.get("directive_verb")
            if verb:
                VERB_TO_TEMPLATE[verb] = (spec["id"], review_flag)
            # The palette speaks English, so the directive grammar does too:
            # every pack answers to its own id and to any alias it declares.
            # The Arabic verbs above keep working, so directive lists already
            # written still parse exactly as they did.
            for alias in [spec["id"], *spec.get("directive_aliases", [])]:
                VERB_TO_TEMPLATE.setdefault(str(alias).lower(), (spec["id"], review_flag))
        except Exception as e:
            TEMPLATE_ERRORS[d.name] = str(e)
            print(f"[templates] skipping {d.name}: {e}")
    # Grammar-level verbs not owned by a pack:
    if "meme-frame" in TEMPLATE_PACKS:
        VERB_TO_TEMPLATE.setdefault("ميم", ("meme-frame", True))
    elif "image-pop" in TEMPLATE_PACKS:
        VERB_TO_TEMPLATE["ميم"] = ("image-pop", True)
    VERB_TO_TEMPLATE["مولّد"] = ("__placeholder__", True)
    VERB_TO_TEMPLATE["مولد"] = ("__placeholder__", True)
    for alias in ("generate", "placeholder"):
        VERB_TO_TEMPLATE.setdefault(alias, ("__placeholder__", True))


def camera_packs() -> set[str]:
    """Templates that transform the source footage rather than draw over it."""
    return {tid for tid, spec in TEMPLATE_PACKS.items() if spec.get("camera")}


# --------------------------------------- [4] Template foley event resolution
def sfx_repeat_count(item: Instance, field_name: str) -> int:
    """How many times a repeating sound fires for this instance.

    A pack points `repeat_field` at whatever it actually repeats over: a list of
    strokes, a multi-line text field of list items, or a plain number. Reading
    all three here means a template author never has to add a hidden count field
    just to make its foley line up with what is on screen.
    """
    if not field_name:
        return 1
    value = item.fields.get(field_name)
    if isinstance(value, list):
        return max(1, len(value))
    if isinstance(value, str):
        parts = [part for part in re.split(r"\r?\n|\s*\|\s*", value) if part.strip()]
        return max(1, len(parts))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(1, min(64, int(value)))
    return 1


# The counting ladder. A number that climbs has to be *read*, and a mechanical
# counter does not glide - it advances a notch at a time, quickly at first and
# then slower as it settles on the figure. `steps` cuts the climb into that many
# notches and `ratio` is how much longer each gap is than the one before it, so
# at ratio 1.145 the first notch lasts 50 ms and the last one 170 ms. The
# positions are a geometric series normalised onto the value window, which makes
# them exactly invertible: the same two lines give the picture its step times
# and the foley its tick times, so every tick you hear is a digit you see
# changing. An easing curve cannot do this job - inverting one puts most of the
# ticks in the first fifth and then leaves half a second of silence before the
# final one, because the tail of an ease-out is flat by design.
DEFAULT_VALUE_MOTION = {
    "easing": "outQuint", "ms": 0, "min_ms": 900, "delay_ms": 0,
    "steps": 0, "ratio": 1.0, "steps_from": None,
}


def value_number(raw: Any) -> float:
    """The number inside a field the editor typed by hand.

    Mirrors the expression the packs use, so "1,200" and "$1200" and "1200" are
    all twelve hundred and an empty box is zero rather than an error.
    """
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(raw if raw is not None else "")) or 0)
    except ValueError:
        return 0.0


def value_steps(spec: dict[str, Any], item: Instance) -> int:
    """How many notches this block's count-up climbs in.

    Normally the pack decides, because how a count *feels* is the pack's
    business. A pack that declares `motion.value.steps_from` hands that decision
    to the block instead: it names the fields holding the figure, the number to
    start from and how much to increase by, and the ladder is however many
    increments that is. The difference is what the viewer reads - a count of ten
    even tenths of 1,240 goes 124, 248, 372, which is arithmetic nobody
    recognises, while counting up by 200 goes 200, 400, 600 and reads as a
    counter. The times stay geometric either way, so it still decelerates.
    """
    motion = spec.get("motion") or {}
    value = {**DEFAULT_VALUE_MOTION, **(motion.get("value") or {})}
    steps = max(0, min(64, int(value.get("steps") or 0)))
    keys = value.get("steps_from") or {}
    if not keys:
        return steps
    by = abs(value_number(item.fields.get(keys.get("step"))))
    if by <= 0:
        return steps
    span = abs(value_number(item.fields.get(keys.get("value")))
               - value_number(item.fields.get(keys.get("start"))))
    return max(1, min(64, round(span / by))) if span > 0 else steps


def value_ramp(spec: dict[str, Any], item: Instance,
               element: str = "") -> tuple[float, float, dict[str, Any]]:
    """When a quantity starts climbing and how long it climbs for.

    A mirror of `valueAt()` in index.html, kept here because the audio mix is
    built in Python while the picture is drawn in the browser. `_test_value_ladder()`
    and the stat step in tools/test_ui.py assert the two against each other.
    """
    motion = spec.get("motion") or {}
    value = {**DEFAULT_VALUE_MOTION, **(motion.get("value") or {})}
    stagger = motion.get("stagger") or {}
    elements = list(stagger.get("elements") or [])
    index = elements.index(element) if element in elements else 0
    delay = (index * float(stagger.get("step_ms", 0)) + float(value["delay_ms"])) / 1000
    entrance = float((motion.get("in") or {}).get("ms", 340)) / 1000
    wanted = float(value["ms"]) / 1000 or max(entrance, float(value["min_ms"]) / 1000)
    # Never still climbing when the block starts to leave: a number that never
    # reaches its value is worse than one that arrives early.
    room = max(.15, item.duration * .65 - delay)
    return delay, max(.05, min(wanted, room)), value


def value_step_times(spec: dict[str, Any], item: Instance, element: str = "") -> list[float]:
    """Times, relative to the block, at which a stepped count-up advances a notch."""
    delay, window, value = value_ramp(spec, item, element)
    steps = value_steps(spec, item)
    if steps < 1:
        return []
    ratio = float(value.get("ratio") or 1.0)
    span = ratio ** steps - 1 if ratio > 1.0001 else 0.0
    # k starts at 1: the times are the moments the number *changes*, and there
    # are `steps` of those, not `steps + 1`. The last one is the arrival, so the
    # final tick and the final digit are the same instant.
    return [delay + window * ((ratio ** k - 1) / span if span else k / steps)
            for k in range(1, steps + 1)]


def resolve_sfx_events(item: Instance, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve one template instance into deterministic preview/export events.

    A silenced block resolves to nothing at all rather than to muted events,
    and a `lite` one drops the pack's opening hit the same way. The timeline
    draws its SFX markers from this same list, so what is not heard leaves no
    marker either and the track stays an honest picture of the export.
    """
    if item.foley == "off":
        return []
    events: list[dict[str, Any]] = []
    for sound in spec.get("sfx", []):
        # A `lead` sound is the hit that opens the block rather than part of
        # its body, and `lite` is the block without it.
        if sound.get("lead") and item.foley != "full":
            continue
        if "follow_value" in sound:
            # A sound tied to a counting number: one tick per notch of the
            # ladder, so the ticking decelerates exactly as the digits do.
            offset = float(sound.get("offset", 0))
            for index, step in enumerate(value_step_times(
                    spec, item, str(sound.get("follow_value") or ""))):
                relative = step + offset
                if relative <= item.duration:
                    events.append({
                        "time": item.start + relative,
                        "file": sound["file"],
                        "path": sfx_file(spec, sound["file"]),
                        "gain": float(sound.get("gain", 0.5)),
                        "index": index,
                    })
            continue
        count = sfx_repeat_count(item, sound.get("repeat_field", ""))
        if "offset_ratio" in sound:
            base = item.duration * float(sound["offset_ratio"])
        else:
            base = float(sound.get("offset", 0))
        spread = item.duration * float(sound.get("spread_ratio", 0))
        for index in range(count):
            relative = base
            if count > 1:
                relative += (
                    spread * index / max(1, count - 1)
                    if spread
                    else float(sound.get("repeat_spacing", 0)) * index
                )
            if relative <= item.duration:
                events.append({
                    "time": item.start + relative,
                    "file": sound["file"],
                    "path": sfx_file(spec, sound["file"]),
                    "gain": float(sound.get("gain", 0.5)),
                    "index": index,
                })
    return events


# --------------------------------------------------- [5] Media helpers
def probe(path: Path) -> MediaInfo:
    p = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    info = MediaInfo()
    try:
        if p.returncode:
            raise ValueError((p.stderr or "ffprobe failed").strip())
        j = json.loads(p.stdout)
        info.duration = float(j.get("format", {}).get("duration", 0) or 0)
        for s in j.get("streams", []):
            if s.get("codec_type") == "video" and not info.codec:
                info.codec = s.get("codec_name", "")
                info.width = int(s.get("width", 0)); info.height = int(s.get("height", 0))
                rate = s.get("avg_frame_rate") or s.get("r_frame_rate", "30/1")
                num, _, den = rate.partition("/")
                info.fps = round(float(num) / max(float(den or 1), 1), 3)
                info.profile = str(s.get("profile", "") or "")
                info.pix_fmt = str(s.get("pix_fmt", "") or "")
                info.bit_rate = int(float(s.get("bit_rate") or 0))
            if s.get("codec_type") == "audio" and not info.audio_codec:
                info.audio_codec = s.get("codec_name", "")
    except Exception as exc:
        raise ValueError(f"Could not read media metadata for {path.name}: {exc}") from exc
    return info


def renderer_executable() -> Optional[Path]:
    """Return the installed private Playwright browser executable, if present."""
    roots = []
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured and configured != "0":
        roots.append(Path(configured))
    roots.append(Path.home() / "AppData" / "Local" / "ms-playwright")
    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe",
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-win64/chrome.exe",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            match = next(root.glob(pattern), None)
            if match and match.is_file():
                return match
    return None


def waveform_peaks(name: str) -> list[float]:
    """Server-side peaks: decode audio to raw s16 mono 8kHz, take max-abs per window. Cached."""
    d = project_dir(name)
    cache = d / "waveform.json"
    if cache.exists():
        return json.loads(cache.read_text())
    st = load_state(name)
    if not st.source:
        return []
    src = d / st.source
    p = subprocess.run([FFMPEG, "-v", "quiet", "-i", str(src), "-ac", "1", "-ar", "8000",
                        "-f", "s16le", "-"], capture_output=True, creationflags=CHILD_FLAGS)
    raw = p.stdout
    if p.returncode or not raw:
        cache.write_text("[]", encoding="utf-8")
        return []
    n = len(raw) // 2
    target = 4000                                # ~4000 peak columns total
    win = max(1, n // target)
    peaks = []
    for i in range(0, n - win, win):
        chunk = raw[i * 2:(i + win) * 2]
        mx = 0
        for j in range(0, len(chunk), 2):
            v = abs(struct.unpack_from("<h", chunk, j)[0])
            if v > mx: mx = v
        peaks.append(round(mx / 32768, 3))
    cache.write_text(json.dumps(peaks), encoding="utf-8")
    return peaks


def thumbnail(name: str, t: float) -> Path:
    d = project_dir(name); (d / "thumbs").mkdir(exist_ok=True)
    out = d / "thumbs" / f"{t:.2f}.jpg"
    if not out.exists():
        st = load_state(name)
        if not st.source:
            raise ValueError("project has no source video")
        run([FFMPEG, "-y", "-ss", str(max(0, t)), "-i", str(d / st.source), "-frames:v", "1",
             "-vf", "scale=240:-2", str(out)], check=True)
    return out


def raster_pdf(name: str, asset: str, page: int) -> Path:
    """Rasterize one PDF page to PNG via PyMuPDF, cached in assets/."""
    import fitz  # PyMuPDF
    d = project_dir(name)
    asset_name = Path(asset).name
    source = (d / "assets" / asset_name).resolve()
    assets_dir = (d / "assets").resolve()
    if assets_dir not in source.parents or source.suffix.lower() != ".pdf" or not source.is_file():
        raise ValueError("invalid PDF asset")
    out = d / "assets" / f"{source.stem}_p{page}.png"
    if not out.exists():
        doc = fitz.open(source)
        if page < 0 or page >= len(doc):
            raise ValueError("PDF page is out of range")
        pix = doc[page].get_pixmap(dpi=180)
        pix.save(str(out))
    return out


# ------------------------------ [6] Directive parser (da7ee7-director grammar)
DIRECTIVE_GRAMMAR = """
Grammar (plain text, one directive per line):

  [anchor] VERB description...

anchor  = timestamp and/or a verbatim "quote" from the transcript.
          Timestamp forms tolerated:  [mm:ss]   mm:ss–mm:ss   (mm:ss)   [h:mm:ss]
          Quote forms tolerated:      "..."  «...»  “...”
          If only a quote is given, its timestamp is located in the imported
          word-level transcript (first fuzzy match of the word sequence).
VERB    = the template to place. Any template id works as a verb — keyword,
          image-pop, stat-pop, screen, punch-in, video-clip — as does
          `generate` for a placeholder to fill in later. Verbs are matched
          case-insensitively, as whole words, and never inside the quote.
          The original Arabic verbs remain accepted as aliases:
          نص → keyword · صورة → image-pop · ب-رول → video-clip · سكرين → screen
          زوم → punch-in · ميم → image-pop (+review ⚠️) · مولّد → placeholder
Lines that cannot be parsed are collected into a review list — never dropped.
For keyword, the text field = the words inside the quote (verbatim spoken words).
"""

TS = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})"
TS_ANY = re.compile(r"[\[\(]?\s*" + TS + r"\s*(?:[–\-—]\s*" + TS + r")?\s*[\]\)]?")
QUOTE = re.compile(r'["“«]([^"”»]+)["”»]')
VERB_RE = None  # built at scan time


def _ts_to_sec(h, m, s) -> float:
    return (int(h or 0)) * 3600 + int(m) * 60 + int(s)


def normalize_quote(text: str) -> str:
    """Fold punctuation, diacritics and spacing so quotes match how they sound."""
    folded = unicodedata.normalize("NFKD", str(text or "")).casefold()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[\u0640\u064b-\u0652]", "", folded)
    folded = re.sub(r"[^\w\s\u0600-\u06ff]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def find_quote_time(quote: str, captions: list[CaptionBlock]) -> Optional[float]:
    words = [w for w in re.findall(r"[\w']+", quote.lower())]
    if not words:
        return None
    flat: list[CaptionWord] = [w for b in captions for w in b.words] or \
        [CaptionWord(w=t, s=b.start, e=b.end) for b in captions for t in b.text.lower().split()]
    toks = [re.sub(r"\W", "", w.w.lower()) for w in flat]
    first = re.sub(r"\W", "", words[0])
    for i, t in enumerate(toks):
        if t == first:
            ok = sum(1 for k, wd in enumerate(words[1:4], 1)
                     if i + k < len(toks) and toks[i + k] == re.sub(r"\W", "", wd))
            if len(words) == 1 or ok >= min(2, len(words) - 1):
                return flat[i].s
    return None


def parse_directives(text: str, st: ProjectState) -> tuple[list[Instance], list[str]]:
    global VERB_RE
    verbs = sorted(VERB_TO_TEMPLATE.keys(), key=len, reverse=True)
    if not verbs:
        return [], [line for line in text.splitlines() if line.strip()]
    # English verbs are ordinary words, so they only count as a command when
    # they stand alone: "screen" is a directive, the "screen" inside
    # "screenshot" is not. Arabic verbs keep matching as bare substrings, which
    # is how every directive list written so far parses.
    VERB_RE = re.compile("(" + "|".join(
        rf"\b{re.escape(v)}\b" if v.isascii() else re.escape(v) for v in verbs) + ")",
        re.IGNORECASE)
    out, review = [], []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-•*·").strip()
        if not line:
            continue
        # The quote is what the speaker said, not an instruction, so the verb
        # is looked for everywhere except inside it. Blanking the quoted span
        # rather than removing it keeps every offset lined up with `line`.
        masked = QUOTE.sub(lambda m: " " * len(m.group(0)), line)
        vm = VERB_RE.search(masked)
        tm = TS_ANY.search(line)
        qm = QUOTE.search(line)
        t = None
        if tm:
            t = _ts_to_sec(tm.group(1), tm.group(2), tm.group(3))
        elif qm:
            t = find_quote_time(qm.group(1), st.captions)
        if vm is None or t is None:
            review.append(raw); continue
        tid, flag = VERB_TO_TEMPLATE[vm.group(1).lower()]
        if tid == "__placeholder__":
            tid = "image-pop" if "image-pop" in TEMPLATE_PACKS else "keyword"
        spec = TEMPLATE_PACKS.get(tid, {})
        fields: dict[str, Any] = {}
        needs_asset = any(f["type"].startswith("asset") for f in spec.get("fields", []))
        if tid == "keyword" and qm:
            fields["text"] = qm.group(1)
        description = line[vm.end():].strip(" :-–—")
        if description:
            fields["description"] = description
        fields["_directive_line"] = raw
        duration = float(spec.get("default_duration", 3.0))
        if tm and tm.group(4) is not None:
            end = _ts_to_sec(tm.group(4), tm.group(5), tm.group(6))
            if end > t:
                duration = end - t
        inst = Instance(
            id=f"i-{uuid.uuid4().hex[:12]}", template=tid, start=round(t, 3),
            duration=duration, fields=fields,
            missing=needs_asset, review=flag,
            backdrop=bool(spec.get("backdrop")),
            x=spec.get("zones", {}).get(st.orientation, {}).get("x", 0.5),
            y=spec.get("zones", {}).get(st.orientation, {}).get("y", 0.3),
            scale=spec.get("zones", {}).get(st.orientation, {}).get("scale", 1.0),
        )
        out.append(inst)
    return out, review


def _test_parser():
    st = ProjectState(name="t", captions=[CaptionBlock(id="c", start=12, end=14, text="hello brave new world",
        words=[CaptionWord(w=w, s=12 + i, e=12.5 + i) for i, w in enumerate("hello brave new world".split())])])
    scan_templates()
    ok, rv = parse_directives('[0:05] نص "hello"\n"brave new" زوم tighten\n(1:02) ميم reaction\ngarbage line', st)
    assert len(ok) == 3 and len(rv) == 1, (ok, rv)
    assert ok[0].template == "keyword" and ok[0].start == 5 and ok[0].fields["text"] == "hello"
    assert ok[1].template == "punch-in" and ok[1].start == 13
    assert ok[2].review is True and ok[2].missing is True

    # English verbs, matched case-insensitively and as whole words.
    ok, rv = parse_directives(
        '[0:05] keyword "hello"\n[0:08] Image-Pop a diagram\n[0:11] stat-pop 40', st)
    assert len(ok) == 3 and not rv, (ok, rv)
    assert [i.template for i in ok] == ["keyword", "image-pop", "stat-pop"]

    # A verb inside the quote is speech, not a command: this line is a keyword
    # card, not a screen recording, and "screenshot" must not trigger `screen`.
    ok, rv = parse_directives('[0:20] keyword "put it on the screen"\n'
                              '[0:30] image-pop screenshot of the dashboard', st)
    assert len(ok) == 2 and not rv, (ok, rv)
    assert [i.template for i in ok] == ["keyword", "image-pop"]
    assert ok[0].fields["text"] == "put it on the screen"
    print("parser tests OK")


# ----------------------------------------------------------- [7] HTTP API
scan_templates()
app = FastAPI(title="Editoro", version=APP_VERSION)


SENTENCE_END = re.compile(r"[.!?\u061f\u2026]$")


def group_caption_words(
    words: list[CaptionWord],
    mode: str = "chunk",
    words_per_chunk: int = 7,
) -> list[CaptionBlock]:
    """Group word timestamps into readable, editable caption blocks.

    `chunk` keeps captions short enough to read at a glance; `sentence` holds a
    whole sentence on screen, which suits slower, more written-sounding videos.
    Both keep the word-level timings, so the active-word highlight still works
    and a later regroup is never lossy.
    """
    unique: dict[tuple[float, float, str], CaptionWord] = {}
    for word in words:
        if word.w and word.e > word.s:
            unique[(round(word.s, 3), round(word.e, 3), word.w)] = word
    ordered = sorted(unique.values(), key=lambda item: (item.s, item.e))
    captions: list[CaptionBlock] = []
    buffer: list[CaptionWord] = []

    def flush() -> None:
        if not buffer:
            return
        captions.append(CaptionBlock(
            id=f"c-{uuid.uuid4().hex[:10]}",
            start=buffer[0].s,
            end=buffer[-1].e,
            text=" ".join(item.w for item in buffer),
            words=list(buffer),
        ))
        buffer.clear()

    limit = max(1, min(24, int(words_per_chunk)))
    for word in ordered:
        candidate = " ".join([*(item.w for item in buffer), word.w])
        previous_ended = bool(buffer and SENTENCE_END.search(buffer[-1].w))
        gap = word.s - buffer[-1].e if buffer else 0
        if mode == "sentence":
            # Only a finished sentence, a long silence, or an unreadably long
            # run breaks the block; that is the whole point of the mode.
            should_break = buffer and (
                previous_ended or gap > 1.1 or len(buffer) >= 42
                or word.e - buffer[0].s > 12.0
            )
        else:
            should_break = buffer and (
                len(buffer) >= limit
                or len(candidate) > max(24, limit * 8)
                or word.e - buffer[0].s > 3.5
                or gap > 0.65
                or previous_ended
            )
        if should_break:
            flush()
        buffer.append(word)
    flush()
    return captions


def parse_transcript(data: str, filename: str) -> list[CaptionBlock]:
    """Parse SRT or faster-whisper JSON into compact editable caption blocks."""
    captions: list[CaptionBlock] = []
    if filename.lower().endswith(".srt"):
        normalized = data.replace("\r\n", "\n").replace("\r", "\n")
        pattern = re.compile(
            r"(?:^|\n)\s*\d*\s*\n?"
            r"(\d+:\d+:\d+[.,]\d+)\s*-->\s*(\d+:\d+:\d+[.,]\d+)[^\n]*\n"
            r"(.+?)(?=\n{2,}|\Z)", re.S
        )

        def seconds(value: str) -> float:
            h, minute, second = value.replace(",", ".").split(":")
            return int(h) * 3600 + int(minute) * 60 + float(second)

        for match in pattern.finditer(normalized):
            start, end = seconds(match.group(1)), seconds(match.group(2))
            text = re.sub(r"<[^>]+>", "", " ".join(match.group(3).split())).strip()
            if text and end > start:
                captions.append(CaptionBlock(
                    id=f"c-{uuid.uuid4().hex[:10]}", start=start, end=end, text=text
                ))
    else:
        payload = json.loads(data)
        if isinstance(payload, dict):
            segments = payload.get("segments", [])
            raw_words = payload.get("words", [])
        elif isinstance(payload, list):
            segments, raw_words = payload, []
        else:
            raise ValueError("transcript JSON must contain segments or words")
        words: list[CaptionWord] = []
        for raw in raw_words:
            words.append(CaptionWord(
                w=str(raw.get("word", raw.get("w", ""))).strip(),
                s=float(raw.get("start", raw.get("s", 0))),
                e=float(raw.get("end", raw.get("e", 0))),
                p=float(raw.get("probability", raw.get("p", 1))),
            ))
        for segment in segments:
            segment_words = segment.get("words", []) if isinstance(segment, dict) else []
            if segment_words:
                for raw in segment_words:
                    words.append(CaptionWord(
                        w=str(raw.get("word", raw.get("w", ""))).strip(),
                        s=float(raw.get("start", raw.get("s", 0))),
                        e=float(raw.get("end", raw.get("e", 0))),
                        p=float(raw.get("probability", raw.get("p", 1))),
                    ))
            elif isinstance(segment, dict) and segment.get("text"):
                start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
                text = str(segment["text"]).strip()
                if text and end > start:
                    captions.append(CaptionBlock(
                        id=f"c-{uuid.uuid4().hex[:10]}", start=start, end=end, text=text
                    ))
        captions.extend(group_caption_words(words))
    captions.sort(key=lambda item: item.start)
    if not captions:
        raise ValueError("no timed captions were found")
    return captions


@app.get("/")
def index():
    return HTMLResponse((ROOT / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def api_health():
    return {
        "ok": True,
        "app": "Editoro",
        "version": APP_VERSION,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "templates": len(TEMPLATE_PACKS),
    }


@app.get("/api/diagnostics")
def api_diagnostics():
    renderer = renderer_executable()
    return {
        **api_health(),
        "nvenc": nvenc_available(),
        "whisper": importlib.util.find_spec("faster_whisper") is not None,
        "whisper_models": str(WHISPER_MODELS_DIR),
        # The Look panel needs a runtime now and a ~400 MB network on first use.
        # Reporting both separately is the difference between "install the
        # dependency" and "the first analysis will take a minute longer".
        "depth_runtime": importlib.util.find_spec("onnxruntime") is not None,
        "depth_model": depth_model_ready(),
        "cuda_gpu": bool(shutil.which("nvidia-smi")),
        # Empty unless the machine has a GPU the depth runtime cannot see, which
        # is the one configuration that costs hours and reports nothing.
        "depth_warning": depth_provider_warning(),
        "renderer": bool(renderer),
        "renderer_path": str(renderer) if renderer else "",
        "python": sys.version.split()[0],
        "root": str(ROOT),
        "projects": len([
            p for p in PROJECTS_DIR.iterdir()
            if not p.name.startswith(".deleted-") and (p / "project.json").is_file()
        ]),
        "template_ids": sorted(TEMPLATE_PACKS),
        "template_errors": TEMPLATE_ERRORS,
        "template_renderers": {
            template_id: bool(spec.get("_has_render"))
            for template_id, spec in sorted(TEMPLATE_PACKS.items())
        },
    }


# Frames on their way out of the browser. See EXPORT_FRAME_SINK below.
EXPORT_FRAMES: dict[str, dict[int, bytes]] = {}


@app.post("/api/export/frame/{token}/{seq}")
async def api_export_frame(token: str, seq: int, request: Request):
    """One rendered overlay frame, posted back by the headless renderer.

    Returning frames as base64 data URLs through page.evaluate is what made a
    4K export unusable. Chromium encodes the PNG, expands it by a third into
    base64, then serialises a whole batch of those into a single CDP JSON
    message - about 58 MB of string per eight frames at 2160x3840, and the
    measured cost was seconds per frame, dwarfing the PNG encode itself.

    Posting each frame back as a binary body instead keeps the same PNG and the
    same FFmpeg input format, and simply stops paying for base64 and for JSON.
    The page and this server are the same origin, so no CORS, no upgrade
    handshake, and one ordinary route.
    """
    sink = EXPORT_FRAMES.get(token)
    if sink is None:
        # Either the export was canceled while a frame was in flight, or this
        # page is posting to a different Editoro process than the one running
        # its export. Both are survivable - the renderer falls back to a data
        # URL - but the second is worth being able to see, because it makes
        # every export mysteriously slow and nothing else would mention it.
        print(f"[export] frame {seq} arrived for an unknown session {token[:8]}; "
              f"{len(EXPORT_FRAMES)} session(s) known here",
              file=sys.stderr, flush=True)
        raise HTTPException(410, "that export is no longer running")
    sink[seq] = await request.body()
    return {"ok": True}


@app.get("/api/templates")
def api_templates():
    return {"packs": list(TEMPLATE_PACKS.values()), "shared": shared_assets()}


@app.get("/api/projects")
def api_projects():
    projects = []
    for folder in PROJECTS_DIR.iterdir():
        if (not folder.name.startswith(".deleted-")
                and folder.is_dir() and (folder / "project.json").is_file()):
            try:
                state = load_state(folder.name)
                projects.append({
                    "name": state.name,
                    "source": state.source,
                    "duration": state.source_info.duration,
                    "orientation": state.orientation,
                    "updated": (folder / "project.json").stat().st_mtime,
                })
            except Exception:
                projects.append({"name": folder.name, "error": "Project state needs repair"})
    return {"projects": sorted(projects, key=lambda item: item["name"])}


@app.post("/api/projects/{name}")
def api_new_project(name: str):
    try:
        d = project_dir(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if (d / "project.json").exists():
        raise HTTPException(409, "A project with this name already exists")
    d.mkdir(parents=True, exist_ok=True)
    (d / "assets").mkdir(exist_ok=True); (d / "exports").mkdir(exist_ok=True)
    save_state(load_state(name))
    return {"ok": True}


@app.delete("/api/projects/{name}")
def api_delete_project(name: str):
    d = require_project(name)
    if name in EXPORT_CANCEL:
        raise HTTPException(409, "Cancel the running export before deleting this project")
    transcription = TRANSCRIPTION_JOBS.get(name)
    if transcription and transcription.get("status") in {"queued", "loading", "transcribing", "canceling"}:
        raise HTTPException(409, "Cancel the running transcription before deleting this project")
    trash = PROJECTS_DIR / f".deleted-{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    d.rename(trash)
    return {"ok": True, "recoverable_at": trash.name}


@app.get("/api/projects/{name}/state")
def api_state(name: str):
    require_project(name)
    return JSONResponse(load_state(name).model_dump())


@app.post("/api/projects/{name}/state")
async def api_save(name: str, req: Request):
    """Replace the project state.

    The saved state is broadcast to every connected client, so an edit written
    here - by the MCP server, a script, or another device - appears immediately
    in an open browser instead of waiting for a reload.
    """
    require_project(name)
    payload = await req.json()
    payload["name"] = name
    st = ProjectState.model_validate(payload)
    st = save_state(st)
    await HUB.broadcast({"type": "state", "state": st.model_dump()})
    return {"ok": True, "state": st.model_dump()}


@app.post("/api/projects/{name}/source")
async def api_source(name: str, file: UploadFile):
    d = require_project(name)
    transcription = TRANSCRIPTION_JOBS.get(name)
    if transcription and transcription.get("status") in {"queued", "loading", "transcribing", "canceling"}:
        raise HTTPException(409, "Cancel transcription before replacing the source")
    filename = safe_upload_name(file.filename, VIDEO_EXTENSIONS)
    suffix = Path(filename).suffix.lower()
    incoming = d / f".source-upload-{uuid.uuid4().hex}{suffix}"
    try:
        with open(incoming, "wb") as f:
            while chunk := await file.read(1 << 20):
                f.write(chunk)
        info = probe(incoming)
    except ValueError as exc:
        incoming.unlink(missing_ok=True)
        raise HTTPException(415, str(exc)) from exc
    except Exception:
        incoming.unlink(missing_ok=True)
        raise
    if not info.codec or not info.width or not info.duration:
        incoming.unlink(missing_ok=True)
        raise HTTPException(415, "The selected file does not contain a readable video stream")

    st = load_state(name)
    old_source = d / st.source if st.source else None
    dest = d / ("source" + suffix)
    backup = d / f".source-backup-{uuid.uuid4().hex}{suffix}"
    if dest.exists():
        os.replace(dest, backup)
    try:
        os.replace(incoming, dest)
        st.source = dest.name
        st.source_revision = uuid.uuid4().hex
        st.source_info = info
        st.orientation = "vertical" if st.source_info.height > st.source_info.width else "horizontal"
        st.cuts = [Cut(src_in=0, src_out=st.source_info.duration)]
        st = save_state(st)
    except Exception:
        dest.unlink(missing_ok=True)
        if backup.exists():
            os.replace(backup, dest)
        raise
    backup.unlink(missing_ok=True)
    if old_source and old_source != dest:
        old_source.unlink(missing_ok=True)
    (d / "waveform.json").unlink(missing_ok=True)
    # The old proxies describe footage that is no longer here. Remove them
    # before the browser can ask for one, then start the replacement.
    shutil.rmtree(preview_dir(name), ignore_errors=True)
    ensure_preview(name, st)
    return st.model_dump()


@app.post("/api/projects/{name}/asset")
async def api_asset(name: str, file: UploadFile):
    d = require_project(name) / "assets"; d.mkdir(exist_ok=True)
    filename = safe_upload_name(file.filename, ASSET_EXTENSIONS)
    dest = unique_path(d, filename)
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    info = MediaInfo()
    if dest.suffix.lower() in VIDEO_EXTENSIONS:
        try:
            info = probe(dest)
        except ValueError as exc:
            dest.unlink(missing_ok=True)
            raise HTTPException(415, str(exc)) from exc
    return {"file": dest.name, "info": info.model_dump(), "url": f"/media/{name}/assets/{dest.name}"}


@app.get("/api/projects/{name}/assets")
def api_assets(name: str):
    d = require_project(name) / "assets"
    files = []
    if d.exists():
        for path in sorted(d.iterdir()):
            if path.is_file():
                files.append({
                    "name": path.name,
                    "size": path.stat().st_size,
                    "type": path.suffix.lower().lstrip("."),
                    "url": f"/media/{name}/assets/{path.name}",
                })
    return {"files": files}


@app.post("/api/projects/{name}/transcript")
async def api_transcript(name: str, file: UploadFile):
    """Import SRT or word-level JSON (faster-whisper). Groups words into blocks."""
    filename = safe_upload_name(file.filename, {".srt", ".json"})
    data = (await file.read()).decode("utf-8-sig", "replace")
    folder = require_project(name)
    st = load_state(name)
    try:
        st.captions = parse_transcript(data, filename)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"Transcript could not be imported: {exc}") from exc
    suffix = Path(filename).suffix.lower()
    for old in folder.glob("transcript.*"):
        old.unlink(missing_ok=True)
    (folder / f"transcript{suffix}").write_text(data, encoding="utf-8")
    st = save_state(st)
    return st.model_dump()


def project_words(name: str, st: ProjectState) -> list[CaptionWord]:
    """Word timings for a project: from the caption blocks, or the Whisper file."""
    words = [word for block in st.captions for word in block.words]
    if words:
        return words
    generated = project_dir(name) / "transcript.generated.json"
    if generated.is_file():
        try:
            payload = json.loads(generated.read_text(encoding="utf-8"))
            return [CaptionWord(**item) for item in payload.get("words", [])]
        except Exception:
            return []
    return []


@app.post("/api/projects/{name}/captions/regroup")
async def api_regroup_captions(name: str, req: Request):
    """Rebuild caption blocks from the stored word timings.

    Regrouping is only possible because word timings survive editing. Text typed
    over a block clears its words, so those blocks are left exactly as they are
    rather than being silently rewritten back to what Whisper heard.
    """
    require_project(name)
    body = await req.json()
    st = load_state(name)
    mode = body.get("mode", st.caption_style.mode)
    if mode not in {"chunk", "sentence"}:
        raise HTTPException(400, "mode must be chunk or sentence")
    per_chunk = int(body.get("words_per_chunk", st.caption_style.words_per_chunk))
    words = project_words(name, st)
    if not words:
        raise HTTPException(
            400,
            "No word timings are available. Generate captions locally or import a "
            "Whisper JSON transcript first.",
        )
    edited = [block for block in st.captions if not block.words]
    regrouped = group_caption_words(words, mode=mode, words_per_chunk=per_chunk)
    keep = {block.id: block for block in st.captions}
    for block in regrouped:
        previous = keep.get(block.id)
        if previous is not None:
            block.follow_global = previous.follow_global
            block.x, block.y, block.scale = previous.x, previous.y, previous.scale
            block.layouts = dict(previous.layouts)
    st.captions = sorted(regrouped + edited, key=lambda block: block.start)
    st.caption_style.mode = mode
    st.caption_style.words_per_chunk = max(1, min(24, per_chunk))
    return save_state(st)


@app.get("/api/projects/{name}/transcript")
def api_get_transcript(name: str):
    """The full transcript, blocks and words, for agents and external tools."""
    st = load_state(name)
    return {
        "project": name,
        "orientation": st.orientation,
        "duration": timeline_duration(st),
        "caption_style": st.caption_style.model_dump(),
        "blocks": [block.model_dump() for block in st.captions],
        "text": " ".join(block.text for block in st.captions).strip(),
        "words": [word.model_dump() for word in project_words(name, st)],
    }


@app.get("/api/projects/{name}/asset-info")
def api_asset_info(name: str, asset: str):
    """Pixel dimensions, page count and kind for one project asset.

    An agent placing a highlight or sizing an image needs the real dimensions,
    not a guess. This is the cheap call that makes the rest of the placement
    arithmetic exact instead of approximate.
    """
    folder = require_project(name)
    path = (folder / "assets" / asset).resolve()
    if (folder / "assets").resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "asset not found")
    suffix = path.suffix.lower()
    info: dict[str, Any] = {
        "asset": asset, "bytes": path.stat().st_size, "extension": suffix.lstrip("."),
    }
    if suffix == ".pdf":
        import fitz
        with fitz.open(path) as document:
            page = document[0]
            info.update({
                "kind": "pdf", "pages": document.page_count,
                "width": round(page.rect.width), "height": round(page.rect.height),
                "aspect": round(page.rect.width / max(1, page.rect.height), 4),
            })
        return info
    media = probe(path)
    kind = "video" if suffix in VIDEO_EXTENSIONS else "image"
    info.update({
        "kind": kind, "pages": 1,
        "width": media.width, "height": media.height,
        "aspect": round(media.width / max(1, media.height), 4),
        "orientation": "vertical" if media.height > media.width else "horizontal",
    })
    if kind == "video":
        info.update({"duration": media.duration, "fps": media.fps,
                     "has_audio": bool(media.audio_codec)})
    return info


@app.get("/api/projects/{name}/asset-text")
def api_asset_text(name: str, asset: str, page: int = 0, query: str = ""):
    """Text boxes on a page, in the 0..1 coordinates the highlight pack wants.

    PDFs carry their own text geometry, so those boxes are exact. Images have no
    text layer; rather than guess, this reports that and hands back the page
    dimensions so the caller can place strokes by eye against a rendered frame.
    """
    folder = require_project(name)
    path = (folder / "assets" / asset).resolve()
    if (folder / "assets").resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "asset not found")
    if path.suffix.lower() != ".pdf":
        media = probe(path)
        return {
            "asset": asset, "page": 0, "source": "none",
            "width": media.width, "height": media.height, "lines": [],
            "note": ("This asset has no text layer. Render a frame with "
                     "/api/projects/{project}/frame and place strokes from what you see, "
                     "or supply the page as a PDF for exact coordinates."),
        }
    import fitz
    with fitz.open(path) as document:
        if not 0 <= page < document.page_count:
            raise HTTPException(400, "page out of range")
        target = document[page]
        width = max(1.0, target.rect.width)
        height = max(1.0, target.rect.height)
        lines: list[dict[str, Any]] = []
        blocks = target.get_text("dict").get("blocks", [])
        for block in blocks:
            for line in block.get("lines", []):
                text = "".join(span.get("text", "") for span in line.get("spans", []))
                if not text.strip():
                    continue
                x0, y0, x1, y1 = line["bbox"]
                lines.append({
                    "text": text.strip(),
                    "x1": round(x0 / width, 5), "x2": round(x1 / width, 5),
                    "y": round((y0 + y1) / 2 / height, 5),
                    "h": round((y1 - y0) / height, 5),
                })
        if query:
            needle = query.strip().casefold()
            matched = [line for line in lines if needle in line["text"].casefold()]
            if not matched:
                # Fall back to matching on the words, so a phrase spanning two
                # rendered lines still resolves to the lines that carry it.
                tokens = [token for token in needle.split() if len(token) > 2]
                matched = [
                    line for line in lines
                    if tokens and any(token in line["text"].casefold() for token in tokens)
                ]
            lines = matched
        return {
            "asset": asset, "page": page, "source": "pdf",
            "width": round(width), "height": round(height),
            "pages": document.page_count, "query": query, "lines": lines,
        }


@app.get("/api/projects/{name}/find-quote")
def api_find_quote(name: str, q: str, limit: int = 5):
    """Locate spoken words on the timeline.

    Anchoring to what was actually said is the whole point of the directive
    grammar, and it is what an agent needs most: it knows the sentence, not the
    timecode. Matching is punctuation- and diacritic-insensitive so a quote
    copied out of a transcript still lands.
    """
    st = load_state(name)
    if not st.captions:
        raise HTTPException(400, "This project has no captions yet")
    needle = normalize_quote(q)
    if not needle:
        raise HTTPException(400, "Provide some words to search for")
    matches: list[dict[str, Any]] = []
    for index, block in enumerate(st.captions):
        haystack = normalize_quote(block.text)
        if needle in haystack:
            matches.append({"start": block.start, "end": block.end,
                            "text": block.text, "exact": True, "block": block.id})
            continue
        # A quote often spans two caption blocks; join a short window and look
        # again so the caller is not forced to guess the chunking.
        window = " ".join(
            normalize_quote(item.text) for item in st.captions[index:index + 4]
        )
        if needle in window:
            matches.append({"start": block.start, "end": st.captions[
                min(len(st.captions) - 1, index + 3)].end,
                "text": " ".join(item.text for item in st.captions[index:index + 4]),
                "exact": False, "block": block.id})
    if not matches:
        tokens = [token for token in needle.split() if len(token) > 2]
        for block in st.captions:
            haystack = normalize_quote(block.text)
            hits = sum(1 for token in tokens if token in haystack)
            if tokens and hits >= max(1, len(tokens) // 2):
                matches.append({"start": block.start, "end": block.end,
                                "text": block.text, "exact": False,
                                "block": block.id, "partial": True})
    return {"query": q, "matches": matches[:max(1, min(50, limit))],
            "total": len(matches)}


@app.get("/api/projects/{name}/waveform")
def api_waveform(name: str):
    require_project(name)
    return {"peaks": waveform_peaks(name)}


@app.get("/api/projects/{name}/thumb")
def api_thumb(name: str, t: float = 0):
    require_project(name)
    try:
        return FileResponse(thumbnail(name, t))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/projects/{name}/pdf-page")
def api_pdf(name: str, asset: str, page: int = 0):
    require_project(name)
    try:
        return FileResponse(raster_pdf(name, asset, page))
    except (ValueError, IndexError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/projects/{name}/directives")
async def api_directives(name: str, req: Request):
    body = await req.json()
    text = body.get("text", "")
    folder = require_project(name)
    st = load_state(name)
    if len(text) > 1_000_000:
        raise HTTPException(413, "Directive text is too large")
    (folder / "directives.txt").write_text(text, encoding="utf-8")
    inst, review = parse_directives(text, st)
    # Re-applying the paste box refreshes directive-owned blocks instead of
    # silently duplicating them; manually placed blocks are preserved.
    st.instances = [
        item for item in st.instances
        if "_directive_line" not in item.fields
    ]
    # place on first free overlay track (simple greedy per instance)
    for i in inst:
        tr = 1
        while any(o.track == tr and o.start < i.start + i.duration and i.start < o.start + o.duration
                  for o in st.instances):
            tr += 1
        i.track = tr
        st.instances.append(i)
    st.directives_review = review
    st = save_state(st)
    return st.model_dump()


@app.get("/api/projects/{name}/directives")
def api_get_directives(name: str):
    folder = require_project(name)
    state = load_state(name)
    path = folder / "directives.txt"
    return {
        "text": path.read_text(encoding="utf-8") if path.is_file() else "",
        "review": state.directives_review,
    }


# ------------------------------- [8] Range-request media serving (iOS Safari)
@app.get("/media/{name}/{path:path}")
def media(name: str, path: str, request: Request):
    try:
        folder = project_dir(name)
    except ValueError:
        return Response(status_code=404)
    f = (folder / path).resolve()
    if folder not in f.parents or not f.is_file():
        return Response(status_code=404)
    size = f.stat().st_size
    rng = request.headers.get("range")
    ctype = {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "png": "image/png",
             "mkv": "video/x-matroska", "m4v": "video/mp4", "webp": "image/webp", "gif": "image/gif",
             "jpg": "image/jpeg", "jpeg": "image/jpeg", "wav": "audio/wav", "mp3": "audio/mpeg",
             "m4a": "audio/mp4",
             "pdf": "application/pdf"}.get(f.suffix[1:].lower(), "application/octet-stream")
    if rng:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        if not match or (not match.group(1) and not match.group(2)):
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        if not match.group(1):
            amount = min(int(match.group(2)), size)
            start, end = size - amount, size - 1
        else:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1)
        def it():
            with open(f, "rb") as fh:
                fh.seek(start); left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk: break
                    left -= len(chunk); yield chunk
        return StreamingResponse(it(), status_code=206, media_type=ctype, headers={
            "Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1), "Cache-Control": "private, max-age=3600"})
    return FileResponse(f, media_type=ctype, headers={
        "Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"
    })


@app.get("/tpl/{pack}/{path:path}")
def tpl_file(pack: str, path: str):
    f = (TEMPLATES_DIR / pack / path).resolve()
    if TEMPLATES_DIR not in f.parents or not f.is_file():
        return Response(status_code=404)
    # Scripts get revalidated on every load. render.js already carries an mtime
    # in its query string, but the shared kit it imports does not, and a cached
    # copy of that would strand every pack on stale code with nothing on screen
    # to say so. Fonts and images keep the default caching; only code is cheap
    # enough to check each time.
    if f.suffix == ".js":
        return FileResponse(f, headers={"Cache-Control": "no-cache"})
    return FileResponse(f)


# ------------------------- [9] WebSocket: realtime sync + export progress
class Hub:
    def __init__(self): self.clients: set[WebSocket] = set()
    async def join(self, ws): await ws.accept(); self.clients.add(ws)
    def leave(self, ws): self.clients.discard(ws)
    async def broadcast(self, msg: dict, exclude: Optional[WebSocket] = None):
        dead = []
        for c in self.clients:
            if c is exclude: continue
            try: await c.send_json(msg)
            except Exception: dead.append(c)
        for c in dead: self.leave(c)


HUB = Hub()


@app.websocket("/ws")
async def ws(ws: WebSocket):
    await HUB.join(ws)
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "state":                      # debounced autosave from a client
                st = ProjectState.model_validate(msg["state"])
                if not (project_dir(st.name) / "project.json").is_file():
                    raise ValueError("Project not found")
                st = save_state(st)
                msg["state"] = st.model_dump()
                await ws.send_json({"type": "saved", "project": st.name})
                await HUB.broadcast(msg, exclude=ws)            # sync other devices
            elif msg.get("type") == "export":
                export_name = str(msg.get("project", ""))
                require_project(export_name)
                if export_name in EXPORT_CANCEL:
                    raise ValueError("An export is already running for this project")
                transcription = TRANSCRIPTION_JOBS.get(export_name)
                if transcription and transcription.get("status") in {"queued", "loading", "transcribing", "canceling"}:
                    raise ValueError("Wait for transcription to finish before exporting")
                export_mode = msg.get("mode", "lossless")
                export_resolution = msg.get("resolution", "source")
                if export_mode not in {"lossless", "hq"}:
                    raise ValueError("Unknown export mode")
                if export_resolution not in {"source", "1080", "720"}:
                    raise ValueError("Unknown export resolution")
                asyncio.create_task(export(
                    export_name, export_mode, export_resolution,
                ))
            elif msg.get("type") == "cancel_export":
                event = EXPORT_CANCEL.get(msg.get("project", ""))
                if event:
                    event.set()
            elif msg.get("type") == "ping":
                await ws.send_json({"type": "pong", "at": time.time()})
    except WebSocketDisconnect:
        HUB.leave(ws)
    except Exception as exc:
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        finally:
            HUB.leave(ws)


async def progress(project: str, **kw):
    await HUB.broadcast({"type": "export_progress", "project": project, **kw})


# -------------------------------------- [9b] Local Whisper transcription
class TranscriptionCanceled(Exception):
    pass


TRANSCRIPTION_JOBS: dict[str, dict[str, Any]] = {}
CUDA_DLL_HANDLES: list[Any] = []


def _public_transcription(project: str, job: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if not job:
        return {"project": project, "status": "idle", "progress": 0}
    allowed = {
        "status", "progress", "message", "error", "caption_count",
        "device", "model", "language", "started", "finished",
    }
    payload = {key: value for key, value in job.items() if key in allowed}
    payload["project"] = project
    payload["done"] = payload.get("status") == "complete"
    return payload


def _configure_cuda_dlls() -> None:
    """Expose pip-installed NVIDIA DLLs before importing CTranslate2."""
    if os.name != "nt":
        return
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    # CTranslate2 needs cuDNN and cuBLAS; ONNX Runtime's CUDA provider also
    # loads the runtime, cuFFT, cuRAND and the JIT. Missing folders are simply
    # skipped, so this stays correct on a machine with no GPU wheels at all.
    candidates = [site / name / "bin" for name in (
        "cudnn", "cublas", "cuda_runtime", "cufft", "curand",
        "cuda_nvrtc", "nvjitlink",
    )]
    additions = [str(path) for path in candidates if path.is_dir()]
    if additions:
        os.environ["PATH"] = os.pathsep.join([
            *additions, *os.environ.get("PATH", "").split(os.pathsep),
        ])
        if hasattr(os, "add_dll_directory") and not CUDA_DLL_HANDLES:
            for path in candidates:
                if path.is_dir():
                    CUDA_DLL_HANDLES.append(os.add_dll_directory(str(path)))


def _transcribe_attempt(
    source: Path,
    request: TranscriptionRequest,
    device: str,
    compute_type: str,
    cancel: threading.Event,
    notify,
) -> dict[str, Any]:
    from faster_whisper import WhisperModel

    notify(
        status="loading",
        progress=0.01,
        device=f"{device} · {compute_type}",
        message=(
            f"Loading {request.model} on {device}. "
            "The first run may download the model once…"
        ),
    )
    WHISPER_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(
        request.model,
        device=device,
        compute_type=compute_type,
        download_root=str(WHISPER_MODELS_DIR),
    )
    if cancel.is_set():
        raise TranscriptionCanceled()
    segments, info = model.transcribe(
        str(source),
        language=None if request.language == "auto" else request.language,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 180},
        condition_on_previous_text=True,
        initial_prompt=request.terms or None,
        hotwords=request.terms or None,
        temperature=0.0,
        hallucination_silence_threshold=1.0,
    )
    duration = max(0.001, float(getattr(info, "duration", 0) or 0))
    detected = str(getattr(info, "language", request.language))
    words: list[CaptionWord] = []
    raw_segments: list[dict[str, Any]] = []
    notify(
        status="transcribing",
        progress=0.03,
        language=detected,
        message=f"Transcribing locally · language {detected}…",
    )
    for segment in segments:
        if cancel.is_set():
            raise TranscriptionCanceled()
        segment_words: list[dict[str, Any]] = []
        for word in getattr(segment, "words", None) or []:
            text = str(getattr(word, "word", "")).strip()
            start = float(getattr(word, "start", segment.start))
            end = float(getattr(word, "end", segment.end))
            probability = float(getattr(word, "probability", 1) or 0)
            if text and end > start:
                words.append(CaptionWord(w=text, s=start, e=end, p=probability))
                segment_words.append({
                    "word": text,
                    "start": start,
                    "end": end,
                    "probability": probability,
                })
        if not segment_words and str(segment.text).strip() and segment.end > segment.start:
            words.append(CaptionWord(
                w=str(segment.text).strip(),
                s=float(segment.start),
                e=float(segment.end),
                p=1,
            ))
        raw_segments.append({
            "start": float(segment.start),
            "end": float(segment.end),
            "text": str(segment.text).strip(),
            "words": segment_words,
        })
        notify(
            status="transcribing",
            progress=min(0.98, max(0.03, float(segment.end) / duration)),
            message=f"Transcribing locally · {float(segment.end):.0f}s / {duration:.0f}s",
        )
    if cancel.is_set():
        raise TranscriptionCanceled()
    return {
        "words": words,
        "raw": {
            "model": request.model,
            "device": device,
            "compute_type": compute_type,
            "language": detected,
            "language_probability": float(getattr(info, "language_probability", 0) or 0),
            "duration": duration,
            "segments": raw_segments,
        },
        "device": f"{device} · {compute_type}",
        "language": detected,
    }


def _transcribe_source(
    source: Path,
    request: TranscriptionRequest,
    cancel: threading.Event,
    notify,
) -> dict[str, Any]:
    _configure_cuda_dlls()
    failures: list[str] = []
    for device, compute_type in (("cuda", "int8_float16"), ("cpu", "int8")):
        try:
            return _transcribe_attempt(
                source, request, device, compute_type, cancel, notify,
            )
        except TranscriptionCanceled:
            raise
        except Exception as exc:
            failures.append(f"{device}: {exc}")
            if device == "cuda":
                notify(
                    status="loading",
                    progress=0.01,
                    device="cpu · int8",
                    message="GPU transcription is unavailable; retrying locally on CPU…",
                )
    raise RuntimeError(" | ".join(failures)[-3000:])


async def _run_transcription(
    project: str,
    request: TranscriptionRequest,
    source_name: str,
    source_revision: str,
) -> None:
    job = TRANSCRIPTION_JOBS[project]
    loop = asyncio.get_running_loop()

    def notify(**updates) -> None:
        job.update(updates)
        payload = {
            "type": "transcription_progress",
            **_public_transcription(project, job),
        }
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(HUB.broadcast(payload))
        )

    try:
        source = project_dir(project) / source_name
        result = await asyncio.to_thread(
            _transcribe_source,
            source,
            request,
            job["cancel"],
            notify,
        )
        if job["cancel"].is_set():
            raise TranscriptionCanceled()
        captions = group_caption_words(result["words"])
        if not captions:
            raise RuntimeError("Whisper did not find any timed speech")
        current = load_state(project)
        if current.source != source_name or current.source_revision != source_revision:
            raise RuntimeError("The source changed while transcription was running")
        current.captions = captions
        save_state(current)
        raw_target = project_dir(project) / "transcript.generated.json"
        raw_temporary = project_dir(project) / ".transcript.generated.json.tmp"
        raw_temporary.write_text(
            json.dumps(result["raw"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(raw_temporary, raw_target)
        notify(
            status="complete",
            progress=1,
            caption_count=len(captions),
            device=result["device"],
            language=result["language"],
            finished=time.time(),
            message=f"Generated {len(captions)} editable caption blocks",
        )
    except TranscriptionCanceled:
        notify(
            status="canceled",
            progress=job.get("progress", 0),
            finished=time.time(),
            message="Transcription canceled · existing captions kept",
        )
    except Exception as exc:
        notify(
            status="failed",
            error=str(exc),
            finished=time.time(),
            message="Local transcription failed",
        )


@app.get("/api/projects/{name}/transcribe")
def api_transcription_status(name: str):
    require_project(name)
    return _public_transcription(name, TRANSCRIPTION_JOBS.get(name))


@app.post("/api/projects/{name}/transcribe")
async def api_start_transcription(name: str, request: TranscriptionRequest):
    require_project(name)
    state = load_state(name)
    if not state.source:
        raise HTTPException(409, "Import source footage before generating captions")
    current = TRANSCRIPTION_JOBS.get(name)
    if current and current.get("status") in {"queued", "loading", "transcribing", "canceling"}:
        raise HTTPException(409, "A transcription is already running for this project")
    if name in EXPORT_CANCEL:
        raise HTTPException(409, "Wait for the export to finish before transcribing")
    if importlib.util.find_spec("faster_whisper") is None:
        raise HTTPException(
            503,
            "Local Whisper is not installed yet. Close Editoro and run launch.cmd once.",
        )
    TRANSCRIPTION_JOBS[name] = {
        "status": "queued",
        "progress": 0,
        "message": "Preparing local transcription…",
        "model": request.model,
        "language": request.language,
        "started": time.time(),
        "cancel": threading.Event(),
    }
    asyncio.create_task(_run_transcription(
        name, request, state.source, state.source_revision,
    ))
    return JSONResponse(
        _public_transcription(name, TRANSCRIPTION_JOBS[name]),
        status_code=202,
    )


@app.post("/api/projects/{name}/transcribe/cancel")
async def api_cancel_transcription(name: str):
    require_project(name)
    job = TRANSCRIPTION_JOBS.get(name)
    if job and job.get("status") in {"queued", "loading", "transcribing"}:
        job["cancel"].set()
        job["status"] = "canceling"
        job["message"] = "Canceling after the current audio segment…"
        await HUB.broadcast({
            "type": "transcription_progress",
            **_public_transcription(name, job),
        })
    return _public_transcription(name, job)


# ------------------------------------------ [9c] Look - defocus and grade
# Two effects, one panel, both derived from the footage rather than dialled in
# by hand. The defocus needs to know how far away every pixel is; the grade
# needs to know what the footage already looks like. Both answers are computed
# once per project, written to disk, and then applied identically by FFmpeg on
# export and by WebGL in the preview - which is the only thing that makes a
# preview of a look worth looking at.
DEPTH_MODEL_REPO = "onnx-community/depth-anything-v3-base"
# Named after the model, not just "depth". Changing which network Editoro uses
# has to invalidate what is already on disk - the files are called the same
# thing in every repository, so a shared folder would quietly keep serving the
# old network under the new name.
DEPTH_MODEL_DIR = ROOT / ".models" / DEPTH_MODEL_REPO.split("/")[-1]
DEPTH_MODEL_FILES = {
    # model.onnx is a 600 KB graph whose weights live beside it in
    # model.onnx_data; ONNX Runtime resolves that by filename, so the two have
    # to land in one folder under exactly these names.
    "model.onnx": f"https://huggingface.co/{DEPTH_MODEL_REPO}/resolve/main/onnx/model.onnx",
    "model.onnx_data": f"https://huggingface.co/{DEPTH_MODEL_REPO}/resolve/main/onnx/model.onnx_data",
}
DEPTH_INPUT_EDGE = 518        # long edge fed to the model; a multiple of 14
DEPTH_RATE = 24.0             # depth inferences per second of footage, on a GPU
DEPTH_RATE_CPU = 8.0          # ...and on a CPU, where the same rate costs hours
DEPTH_SAMPLES = 24            # frames used to calibrate the depth range
DEPTH_BATCH = 8               # frames per inference; sized for a 6 GB card at fp16
MATTE_EDGE = 1080             # long edge of the stored matte
MATTE_VERSION = 2             # bumped when the depth pass changes; older mattes still work
COLOR_SAMPLES = 16            # frames the grade is measured from
COLOR_SAMPLE_EDGE = 320
LUT_SIZE = 33
GRADE_VERSION = 2             # bumped when the grade maths changes; stale cubes are rewritten
GRADE_CONTRAST_FLOOR = 0.09   # every grade gets at least this much S-curve
LOOK_JOBS: dict[str, dict[str, Any]] = {}
DEPTH_SESSION: list[Any] = []   # one lazily built ONNX session per process


def look_dir(project: str) -> Path:
    return project_dir(project) / "look"


def matte_path(project: str) -> Path:
    return look_dir(project) / "matte.mp4"


def cube_path(project: str) -> Path:
    return look_dir(project) / "grade.cube"


def cube_is_current(path: Path) -> bool:
    """Was this .cube written by the grade maths this build ships?"""
    if not path.is_file():
        return False
    try:
        with open(path, encoding="utf-8") as handle:
            for _ in range(4):
                line = handle.readline()
                if not line:
                    break
                if line.startswith("# version:"):
                    return int(line.split(":", 1)[1].strip()) == GRADE_VERSION
    except (OSError, ValueError):
        return False
    return False


def ensure_cube(st: ProjectState) -> None:
    """Rewrite the grade table if it predates the current grade maths.

    The measurement is the expensive half and it does not go stale; only the
    table built from it does. So a build that changes the grade does not cost
    anyone a re-analysis - the next time the table is read, it is rebuilt from
    numbers that are already on disk.
    """
    path = cube_path(st.name)
    if st.look.grade <= 0.001 or not st.look.analysis or cube_is_current(path):
        return
    numbers = {key: value for key, value in st.look.analysis.items()
               if isinstance(value, (int, float))}
    if not numbers:
        return
    try:
        write_cube(path, numbers, st.look.grade)
    except Exception:
        pass          # a grade that cannot be rewritten is not worth failing an export over


def look_state(st: ProjectState) -> dict[str, bool]:
    """Which halves of the look are switched on *and* have a usable artefact."""
    revision = st.source_revision
    defocus = (
        st.look.defocus > 0.001
        and st.look.matte_revision == revision
        and matte_path(st.name).is_file()
    )
    grade = (
        st.look.grade > 0.001
        and st.look.grade_revision == revision
        and cube_path(st.name).is_file()
    )
    return {"defocus": defocus, "grade": grade}


def look_active(st: ProjectState) -> bool:
    flags = look_state(st)
    return flags["defocus"] or flags["grade"]


def _filter_path(path: Path) -> str:
    """A path FFmpeg will accept inside a filter argument.

    Windows drive letters are the problem: a colon separates filter options, so
    `E:/x.cube` parses as an option named `E`. Escaping it is the documented
    fix and is harmless everywhere else.
    """
    return path.as_posix().replace("\\", "/").replace(":", "\\:")


def _smoothstep_lut(low: float, high: float) -> str:
    """A lutyuv expression putting luma through smoothstep(low, high).

    lutyuv evaluates this once per possible value and then uses the table, so
    the cubic costs nothing per pixel - unlike geq, which would evaluate it per
    pixel of every frame.
    """
    span = max(1e-3, high - low)
    return (
        f"st(0,clip((val-{low * 255:.4f})/{span * 255:.4f},0,1));"
        "ld(0)*ld(0)*(3-2*ld(0))*255"
    )


def look_graph(
    st: ProjectState,
    input_label: str,
    output_label: str,
    matte_index: Optional[int],
    pixel_scale: float = 1.0,
) -> str:
    """The defocus + grade chain, from `input_label` to `output_label`.

    It runs *before* the camera move, not after: the matte describes the
    original framing, and a punch-in should magnify an already-defocused frame
    the way a lens does, rather than blur a crop with a mask that no longer
    lines up with it.
    """
    flags = look_state(st)
    parts: list[str] = []
    label = input_label
    if flags["defocus"] and matte_index is not None:
        width = max(2, int(st.source_info.width * pixel_scale))
        height = max(2, int(st.source_info.height * pixel_scale))
        near, far = defocus_sigmas(st.look.defocus, width, height)
        # The matte is stored small and limited-range like any other H.264 file.
        # Expanding it to full range here is what makes these masks agree with
        # the browser's, which does the same expansion in hardware when the
        # matte is uploaded as a texture.
        parts.append(
            f"[{matte_index}:v]setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:flags=bicubic:in_range=tv:out_range=pc,"
            # Eroding the matte pulls the blur a few pixels off the subject.
            # Without it the blur reaches across the silhouette and smears the
            # subject's own edge outward, which reads as a bad cutout; leaving a
            # hair of sharp background behind instead is nearly invisible.
            "format=gray,erosion,erosion,erosion,"
            "tpad=stop=-1:stop_mode=clone,split=2[lknear][lkfar]"
        )
        parts.append(f"[lknear]lutyuv=y='{_smoothstep_lut(*DEFOCUS_NEAR_BAND)}'[lkmn]")
        parts.append(f"[lkfar]lutyuv=y='{_smoothstep_lut(*DEFOCUS_FAR_BAND)}'[lkmf]")
        # Three depth slices - sharp, softened, thrown away - blended by the
        # matte, rather than one cutout pasted onto a blurred plate. That is
        # what gives the wall behind a real falloff instead of a flat card.
        parts.append(f"{label}setpts=PTS-STARTPTS,format=yuv420p,split=3[lk0][lk1][lk2]")
        parts.append(f"[lk1]gblur=sigma={near:.3f}:steps=3[lkg1]")
        parts.append(f"[lk2]gblur=sigma={far:.3f}:steps=3[lkg2]")
        parts.append("[lkg1][lkmn]alphamerge[lka1]")
        parts.append("[lkg2][lkmf]alphamerge[lka2]")
        parts.append("[lk0][lka1]overlay=0:0:format=auto[lko1]")
        parts.append("[lko1][lka2]overlay=0:0:format=auto[lkblur]")
        label = "[lkblur]"
    if flags["grade"]:
        parts.append(
            f"{label}lut3d=file='{_filter_path(cube_path(st.name))}':"
            "interp=tetrahedral[lkgraded]"
        )
        label = "[lkgraded]"
    if not parts:
        return ""
    # Whatever the last stage was, hand it on under the name the caller asked
    # for instead of the internal one.
    parts[-1] = parts[-1].rsplit("[", 1)[0] + output_label
    return ";".join(parts)


def look_inputs(st: ProjectState, src_in: float, duration: float,
                need_matte: bool = False) -> list[str]:
    """The extra `-i` arguments the look needs for one span, or none at all.

    `need_matte` forces the matte in for a span that does not defocus but does
    put a block behind the speaker, which needs the same silhouette.
    """
    if not look_state(st)["defocus"] and not (need_matte and matte_ready(st)):
        return []
    args = [
        "-ss", f"{max(0.0, src_in):.6f}",
        "-t", f"{duration + 0.5:.6f}",
        "-i", str(matte_path(st.name)),
    ]
    if gpu_look_available(st) and look_state(st)["grade"]:
        # A still image looped for as long as the span lasts. It is a video
        # input because that is the only kind of thing a filter graph can hand
        # to a kernel, not because anything about it moves.
        ensure_cube(st)
        args += ["-loop", "1", "-framerate", f"{st.source_info.fps or 30:.6f}",
                 "-t", f"{duration + 0.5:.6f}", "-i", str(write_lut_image(st))]
    return args


def base_video_graph(
    st: ProjectState, seg: dict, fps: float, matte_index: Optional[int] = None
) -> str:
    """Source pixels to `[base]`: the look first, then the camera move."""
    ensure_cube(st)
    if matte_index is not None and gpu_look_available(st):
        graph = look_graph_gpu(st, "[0:v]", "[looked]", matte_index)
    else:
        graph = look_graph(st, "[0:v]", "[looked]", matte_index)
    if not graph:
        return camera_filter_graph(st, seg, fps)
    return graph + ";" + camera_filter_graph(st, seg, fps, "[looked]")


# ------------------------------------------------------- depth and matte
def depth_model_ready() -> bool:
    return all((DEPTH_MODEL_DIR / name).is_file() for name in DEPTH_MODEL_FILES)


def download_depth_model(notify) -> None:
    """Fetch the depth network on first use, the way Whisper models are fetched.

    It is about 100 MB, so it is not something to ship in the repo or to pull
    down during a launch that may never open the Look panel.
    """
    if depth_model_ready():
        return
    import httpx

    DEPTH_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    total_files = len(DEPTH_MODEL_FILES)
    for index, (name, url) in enumerate(DEPTH_MODEL_FILES.items()):
        target = DEPTH_MODEL_DIR / name
        if target.is_file():
            continue
        partial = target.with_name(target.name + ".part")
        notify(status="model", progress=0.02,
               message=f"Downloading the depth model ({index + 1}/{total_files})…")
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            written = 0
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes(1 << 20):
                    handle.write(chunk)
                    written += len(chunk)
                    if total:
                        notify(
                            status="model",
                            progress=0.02 + 0.08 * (index + written / total) / total_files,
                            message="Downloading the depth model… "
                                    f"{written >> 20} of {total >> 20} MB",
                        )
        # Rename only once every byte is there, so an interrupted download
        # cannot leave behind a file that looks complete on the next run.
        os.replace(partial, target)


def half_precision_model(notify=None) -> Optional[Path]:
    """A float16 copy of the depth network, converted once and kept.

    Depth estimation is the one genuinely heavy thing Editoro runs, and the
    network is exported at float32 because that is what runs everywhere. Every
    GPU worth using it on has half-precision tensor cores that are roughly
    twice as fast and need half the memory - and a depth map that feeds an
    8-bit matte cannot tell the difference between the two. So convert once,
    cache the result next to the original, and fall back to float32 if any part
    of that is not available.
    """
    target = DEPTH_MODEL_DIR / "model.fp16.onnx"
    if target.is_file():
        return target
    marker = DEPTH_MODEL_DIR / "model.fp16.unavailable"
    if marker.exists():
        return None
    try:
        import onnx
        from onnxruntime.transformers.float16 import convert_float_to_float16
    except Exception:
        marker.write_text("onnx is not installed\n", encoding="utf-8")
        return None
    if notify:
        notify(status="model", progress=0.08,
               message="Converting the depth model to half precision (once)…")
    partial = DEPTH_MODEL_DIR / "model.fp16.part.onnx"
    data_name = "model.fp16.onnx_data"
    try:
        model = onnx.load(str(DEPTH_MODEL_DIR / "model.onnx"))
        # keep_io_types leaves the inputs and outputs float32, so nothing
        # upstream of this function has to know the weights changed.
        model = convert_float_to_float16(model, keep_io_types=True,
                                         disable_shape_infer=True)
        (DEPTH_MODEL_DIR / data_name).unlink(missing_ok=True)
        onnx.save(model, str(partial), save_as_external_data=True,
                  all_tensors_to_one_file=True, location=data_name)
        os.replace(partial, target)
        return target
    except Exception as error:
        partial.unlink(missing_ok=True)
        (DEPTH_MODEL_DIR / data_name).unlink(missing_ok=True)
        marker.write_text(f"{error}\n", encoding="utf-8")
        return None


def depth_session(notify=None):
    """One ONNX session per process, on the GPU whenever there is one."""
    if DEPTH_SESSION:
        return DEPTH_SESSION[0]
    _configure_cuda_dlls()
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 4
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    available = set(ort.get_available_providers())
    attempts = []
    if "CUDAExecutionProvider" in available:
        attempts.append([("CUDAExecutionProvider", {
            # cuDNN picks its convolution algorithms by trying them; the model
            # runs the same shape thousands of times, so paying for that search
            # once is free and the exhaustive search is the fastest option.
            "cudnn_conv_algo_search": "EXHAUSTIVE",
            "arena_extend_strategy": "kSameAsRequested",
        }), "CPUExecutionProvider"])
    if "DmlExecutionProvider" in available:
        attempts.append(["DmlExecutionProvider", "CPUExecutionProvider"])
    attempts.append(["CPUExecutionProvider"])
    # Half precision is worth having only on a GPU; on the CPU it is slower.
    graphs = []
    if any(p != ["CPUExecutionProvider"] for p in attempts[:-1]):
        half = half_precision_model(notify)
        if half:
            graphs.append(half)
    graphs.append(DEPTH_MODEL_DIR / "model.onnx")
    last: Optional[Exception] = None
    for graph in graphs:
        for providers in attempts:
            if graph.name.startswith("model.fp16") and providers == ["CPUExecutionProvider"]:
                continue
            try:
                session = ort.InferenceSession(str(graph), options, providers=providers)
                DEPTH_SESSION.append(session)
                return session
            except Exception as error:  # a missing CUDA DLL is not fatal here
                last = error
    raise RuntimeError(f"Could not start the depth model: {last}")


def depth_device() -> str:
    session = DEPTH_SESSION[0] if DEPTH_SESSION else None
    providers = session.get_providers() if session else []
    return "gpu" if any(p.startswith(("CUDA", "Dml")) for p in providers) else "cpu"


def depth_provider_warning() -> str:
    """Why the depth pass is about to be slow, when it is about to be slow.

    There is one failure that costs hours and looks like nothing: pip installs
    `onnxruntime` and `onnxruntime-gpu` into the *same* `onnxruntime` package
    directory, so whichever went in last wins. If the CPU wheel lands second it
    silently replaces the GPU build - `import onnxruntime` still works, the
    depth pass still runs, and it runs twenty to fifty times slower on a machine
    with a perfectly good card sitting idle. Nothing anywhere reports it,
    because from the inside a CPU-only runtime on a CPU-only machine and a
    clobbered one look identical.

    So compare the two things that differ: is there a GPU, and can the runtime
    see it. Returns "" when there is nothing to say.
    """
    if importlib.util.find_spec("onnxruntime") is None:
        return ""
    if not shutil.which("nvidia-smi"):
        return ""   # no NVIDIA card; the CPU is genuinely the only option
    try:
        import onnxruntime as ort
        providers = set(ort.get_available_providers())
    except Exception:
        return ""
    if providers & {"CUDAExecutionProvider", "DmlExecutionProvider"}:
        return ""
    return (
        "This machine has an NVIDIA GPU but the installed ONNX Runtime has no "
        "GPU provider, so depth is running on the CPU and will take roughly "
        "twenty times longer. The CPU-only 'onnxruntime' package overwrites the "
        "GPU one. Fix it with:  pip uninstall -y onnxruntime onnxruntime-gpu  "
        "then  pip install onnxruntime-gpu[cuda,cudnn]==1.22.0"
    )


def depth_rate() -> float:
    """Inferences per second of footage, chosen for the hardware in the machine.

    The matte is smoothed and then interpolated up to the source frame rate, so
    this is a quality-versus-time dial rather than a correctness one. On a GPU
    the full rate costs about as long as the video itself and there is no reason
    to give anything up. On a CPU the same rate turns a two-minute clip into an
    afternoon, and a talking head's silhouette simply does not move fast enough
    to need it - so drop to a third and let the interpolation carry the rest.
    """
    return DEPTH_RATE if depth_device() == "gpu" else DEPTH_RATE_CPU


def depth_input_size(width: int, height: int) -> tuple[int, int]:
    """Model input keeping the source aspect, both edges multiples of 14."""
    def step(value: float) -> int:
        return max(14, int(round(value / 14)) * 14)

    if width >= height:
        return DEPTH_INPUT_EDGE, step(DEPTH_INPUT_EDGE * height / max(1, width))
    return step(DEPTH_INPUT_EDGE * width / max(1, height)), DEPTH_INPUT_EDGE


def matte_size(width: int, height: int) -> tuple[int, int]:
    """Stored matte size: small, even, and never larger than the source."""
    scale = min(1.0, MATTE_EDGE / max(1, max(width, height)))
    return (max(2, int(round(width * scale / 2)) * 2),
            max(2, int(round(height * scale / 2)) * 2))


def defocus_sigmas(amount: float, width: int, height: int) -> tuple[float, float]:
    """Blur radii for the mid and far slices, in pixels.

    Bokeh scales with the frame rather than with pixels, so both radii are a
    fraction of the short edge; that is what lets one Amount value look the
    same on 1080p landscape and on 4K vertical.
    """
    far = max(1.0, amount * 0.055 * min(width, height))
    return max(0.6, far * 0.42), far


# Bumped whenever the defocus maths changes. Baked preview copies key off it,
# so a change here rebuilds them instead of leaving the editor showing a look
# the export no longer makes.
DEFOCUS_VERSION = 2
DEFOCUS_NEAR_BAND = (0.14, 0.46)
DEFOCUS_FAR_BAND = (0.44, 0.82)


def _decode_frames(source: Path, filters: str, width: int, height: int, limit: int = 0):
    """Yield raw RGB frames from FFmpeg without writing anything to disk."""
    import numpy as np

    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(source),
               "-vf", filters, "-f", "rawvideo", "-pix_fmt", "rgb24"]
    if limit:
        command += ["-frames:v", str(limit)]
    command += ["pipe:1"]
    size = width * height * 3
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=size * 2,
                               creationflags=CHILD_FLAGS)
    try:
        while True:
            buffer = process.stdout.read(size)
            if not buffer or len(buffer) < size:
                break
            yield np.frombuffer(buffer, np.uint8).reshape(height, width, 3)
    finally:
        if process.stdout:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
        process.wait()


def _infer_depth(session, frames):
    """Run the network over a stack of RGB frames and return the depth maps."""
    import numpy as np

    mean = np.array([0.485, 0.456, 0.406], np.float32)
    std = np.array([0.229, 0.224, 0.225], np.float32)
    batch = (np.stack(frames).astype(np.float32) / 255.0 - mean) / std
    # The network takes [batch, view, channel, y, x]: it can reason about
    # several views of one scene, and a video frame is simply a single view.
    tensor = np.ascontiguousarray(batch.transpose(0, 3, 1, 2)[:, None])
    return session.run(["predicted_depth"], {"pixel_values": tensor})[0][:, 0]


def build_matte(project: str, st: ProjectState, cancel: threading.Event, notify) -> dict[str, Any]:
    """Write a depth matte for the whole source: black is near, white is far."""
    import numpy as np

    source = project_dir(project) / st.source
    width, height = st.source_info.width, st.source_info.height
    duration = st.source_info.duration or 0.0
    if not source.is_file() or not width or not height or duration <= 0:
        raise RuntimeError("The source footage has to be readable before a matte can be built")
    download_depth_model(notify)
    notify(status="depth", progress=0.10, message="Starting the depth model…")
    session = depth_session(notify)
    # Now that a session exists we know what it is running on, so the rate and
    # the estimate can both be honest. Doing this before the first inference
    # matters: the alternative is telling someone "reading depth…" for an hour.
    rate = depth_rate()
    warning = depth_provider_warning()
    if warning:
        notify(status="depth", progress=0.10, message=warning)
    estimate = duration * rate / (31.0 if depth_device() == "gpu" else 0.7)
    notify(status="depth", progress=0.11,
           message=f"Reading depth on the {depth_device().upper()}… "
                   f"this should take about {max(1, round(estimate / 60))} min")
    input_width, input_height = depth_input_size(width, height)
    scale = f"scale={input_width}:{input_height}:flags=bilinear"

    # Pass one: calibrate. Normalising every frame on its own makes the blur
    # pump whenever the deepest thing in shot changes, so the near and far
    # planes are fixed once, from samples spread across the whole video.
    notify(status="depth", progress=0.12, message="Measuring the depth of the shot…")
    sample_rate = max(0.05, DEPTH_SAMPLES / max(1.0, duration))
    samples = []
    for frame in _decode_frames(source, f"fps={sample_rate:.6f},{scale}",
                                input_width, input_height, DEPTH_SAMPLES):
        if cancel.is_set():
            raise LookCanceled()
        samples.append(frame)
    if not samples:
        raise RuntimeError("Could not read any frames from the source footage")
    measured = np.concatenate([
        _infer_depth(session, samples[start:start + DEPTH_BATCH])
        for start in range(0, len(samples), DEPTH_BATCH)
    ])
    near_plane = float(np.percentile(measured, 2))
    far_plane = float(np.percentile(measured, 98))
    if far_plane - near_plane < 1e-3:
        far_plane = near_plane + 1e-3

    matte_width, matte_height = matte_size(width, height)
    look_dir(project).mkdir(parents=True, exist_ok=True)
    target = matte_path(project)
    partial = target.with_name("matte.part.mp4")
    fps = st.source_info.fps or 30
    process = subprocess.Popen([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "gray",
        "-s", f"{input_width}x{input_height}", "-r", f"{rate:.6f}", "-i", "pipe:0",
        # tmix takes the flicker out at the rate the model actually ran, and
        # framerate then blends that up to the source rate - so the matte moves
        # continuously instead of stepping twelve times a second.
        "-vf", (f"tmix=frames=3:weights='1 2 1',framerate=fps={fps:.6f},"
                f"scale={matte_width}:{matte_height}:flags=bicubic,format=yuv420p"),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-g", "30",
        "-movflags", "+faststart", "-an", str(partial),
    ], stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CHILD_FLAGS)
    expected = max(1, int(duration * rate))
    written = 0
    pending: list[Any] = []

    def flush() -> None:
        nonlocal written
        if not pending:
            return
        maps = _infer_depth(session, pending)
        pending.clear()
        for depth in maps:
            # 0 is the plane the subject sits on, 255 is as far as this shot
            # goes. Everything downstream reads the matte that way.
            gray = np.clip((depth - near_plane) / (far_plane - near_plane), 0, 1)
            process.stdin.write((gray * 255).astype(np.uint8).tobytes())
            written += 1

    try:
        for frame in _decode_frames(source, f"fps={rate:.6f},{scale}",
                                    input_width, input_height):
            if cancel.is_set():
                raise LookCanceled()
            pending.append(frame)
            if len(pending) >= DEPTH_BATCH:
                flush()
                notify(status="depth",
                       progress=0.15 + 0.72 * min(1.0, written / expected),
                       message=f"Reading depth… {written} of about {expected} frames")
        flush()
        process.stdin.close()
        if process.wait():
            detail = process.stderr.read().decode("utf-8", "replace")[-400:]
            raise RuntimeError(f"Could not encode the depth matte: {detail}")
    except BaseException:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
        process.wait()
        partial.unlink(missing_ok=True)
        raise
    if not written:
        partial.unlink(missing_ok=True)
        raise RuntimeError("The depth pass produced no frames")
    os.replace(partial, target)
    return {"depth_frames": written, "depth_device": depth_device(),
            "depth_rate": rate,
            "depth_near": round(near_plane, 4), "depth_far": round(far_plane, 4),
            "matte_version": MATTE_VERSION}


# --------------------------------------------------------- automatic grade
def measure_grade(project: str, st: ProjectState) -> dict[str, float]:
    """Read the footage and decide what it needs. No knobs, no presets.

    Everything here is measured from the pixels: what the neutrals are doing,
    where the exposure sits, how much contrast and colour are already present.
    The numbers it returns describe a correction, and `write_cube` bakes them
    into a table. Nothing is applied at full strength - a grade that fully
    neutralises a warm room has removed the room.
    """
    import numpy as np

    source = project_dir(project) / st.source
    width, height = st.source_info.width, st.source_info.height
    duration = st.source_info.duration or 0.0
    if not source.is_file() or not width or not height:
        raise RuntimeError("The source footage has to be readable before it can be graded")
    scale = min(1.0, COLOR_SAMPLE_EDGE / max(1, max(width, height)))
    sample_width = max(2, int(round(width * scale / 2)) * 2)
    sample_height = max(2, int(round(height * scale / 2)) * 2)
    rate = max(0.05, COLOR_SAMPLES / max(1.0, duration))
    frames = list(_decode_frames(
        source, f"fps={rate:.6f},scale={sample_width}:{sample_height}:flags=area",
        sample_width, sample_height, COLOR_SAMPLES))
    if not frames:
        raise RuntimeError("Could not read any frames from the source footage")
    pixels = (np.stack(frames).astype(np.float32) / 255.0).reshape(-1, 3)
    red, green, blue = pixels[:, 0], pixels[:, 1], pixels[:, 2]
    luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    chroma_b = 0.5 - 0.168736 * red - 0.331264 * green + 0.5 * blue
    chroma_r = 0.5 + 0.5 * red - 0.418688 * green - 0.081312 * blue
    # Faces are the one thing in a talking-head frame that must not be used as
    # a neutral reference: average a shot of a person and "grey" comes out
    # skin-coloured, and correcting toward that turns the speaker green.
    skin = ((chroma_b > 0.30) & (chroma_b < 0.50)
            & (chroma_r > 0.52) & (chroma_r < 0.68))
    usable = (luma > 0.05) & (luma < 0.92) & ~skin
    if usable.sum() < pixels.shape[0] // 50:
        usable = (luma > 0.05) & (luma < 0.92)
    neutral = pixels[usable] if usable.any() else pixels
    means = neutral.mean(axis=0) + 1e-5

    def damped(gain: float, share: float = 0.88, limit: float = 0.26) -> float:
        """Move most of the way toward neutral, and never further than `limit`."""
        return float(min(1 + limit, max(1 - limit, 1 + (gain - 1) * share)))

    gain_r = damped(means[1] / means[0])
    gain_b = damped(means[1] / means[2])

    low = float(np.percentile(luma, 1))
    mid = float(np.percentile(luma, 50))
    high = float(np.percentile(luma, 99))
    spread = float(np.percentile(luma, 90) - np.percentile(luma, 10))
    saturation = float(np.mean(pixels.max(axis=1) - pixels.min(axis=1)))
    # Set the black and white points just outside what the footage actually
    # uses, so the correction opens the image up without clipping detail that
    # is really there.
    black = float(min(0.08, max(0.0, low - 0.004)))
    white = float(max(black + 0.35, min(1.0, high + 0.01)))
    lifted = (mid - black) / max(1e-3, white - black)
    gamma = math.log(max(1e-3, 0.46)) / math.log(max(1e-3, min(0.999, lifted)))
    gamma = float(min(1.30, max(0.80, gamma)))
    # A flat picture gets a strong S-curve and a contrasty one gets a light
    # one - but never none at all. The old floor of zero meant well-exposed
    # footage came out of the grade looking exactly like it went in, which is
    # the same thing as the panel not working.
    contrast = float(min(0.42, max(GRADE_CONTRAST_FLOOR, (0.55 - spread) * 1.30)))
    boost = 0.19 / max(0.02, saturation)
    boost = float(min(1.34, max(0.94, 1 + (boost - 1) * 0.80)))
    # Split-tone: cool the shadows, warm the highlights. Nothing measured
    # decides this - it is the one piece of taste in the grade, and it is what
    # separates "corrected" from "graded" to anyone looking at the result.
    # Scaled by how neutral the footage already is, so a shot that is already
    # heavily toned does not get a second tone stacked on top of it.
    tone = float(min(1.0, max(0.25, 1.35 - saturation * 2.4)))
    return {"gain_r": round(gain_r, 5), "gain_b": round(gain_b, 5),
            "black": round(black, 5), "white": round(white, 5),
            "gamma": round(gamma, 5), "contrast": round(contrast, 5),
            "saturation": round(boost, 5), "tone": round(tone, 5),
            "measured_mid": round(mid, 4), "measured_spread": round(spread, 4),
            "measured_saturation": round(saturation, 4)}


def grade_lattice(params: dict[str, float], strength: float, size: int = LUT_SIZE):
    """Apply the measured correction to a colour cube and return the result.

    This is the whole grade, in order: neutralise, set the black and white
    points, place the midtone, add contrast, colour, then tone. Every stage is
    monotonic in isolation, and the two that read luma - the highlight rolloff
    on the white balance and the split-tone - bend the result by well under one
    8-bit code, so a gradient still comes out of the table as a gradient.
    """
    import numpy as np

    # Defaults, so a project analysed by an older build picks up the current
    # grade without having to sit through the analysis pass again.
    params = dict(params)
    params["contrast"] = max(GRADE_CONTRAST_FLOOR, float(params.get("contrast", 0.0)))
    params.setdefault("tone", 0.6)

    axis = np.linspace(0.0, 1.0, size, dtype=np.float32)
    # A .cube varies red fastest, so the lattice has to be built in that order.
    blue_grid, green_grid, red_grid = np.meshgrid(axis, axis, axis, indexing="ij")
    source = np.stack([red_grid, green_grid, blue_grid], axis=-1).reshape(-1, 3)
    rgb = source.copy()

    gains = np.array([params["gain_r"], 1.0, params["gain_b"]], np.float32)
    # White balance belongs in the shadows and midtones. Applied flat, a gain
    # that corrects a warm room also drags every specular highlight toward
    # cyan, which is the single most recognisable sign of an automatic grade -
    # so it rolls off over the top of the range and leaves white white.
    source_luma = (0.2126 * rgb[:, 0] + 0.7152 * rgb[:, 1] + 0.0722 * rgb[:, 2])[:, None]
    keep = np.clip((source_luma - 0.78) / 0.22, 0, 1) ** 2
    rgb = np.clip(rgb * (gains * (1 - keep) + keep), 0, 4)

    black, white = params["black"], params["white"]
    rgb = np.clip((rgb - black) / max(1e-3, white - black), 0, 4)
    rgb = np.power(np.clip(rgb, 0, 1), params["gamma"])

    contrast = params["contrast"]
    if contrast > 0.001:
        # smoothstep is monotonic, so mixing toward it adds contrast without
        # ever crushing one end into a flat patch.
        rgb = rgb * (1 - contrast) + contrast * (rgb * rgb * (3 - 2 * rgb))

    luma = (0.2126 * rgb[:, 0] + 0.7152 * rgb[:, 1] + 0.0722 * rgb[:, 2])[:, None]
    chroma_b = 0.5 - 0.168736 * rgb[:, 0] - 0.331264 * rgb[:, 1] + 0.5 * rgb[:, 2]
    chroma_r = 0.5 + 0.5 * rgb[:, 0] - 0.418688 * rgb[:, 1] - 0.081312 * rgb[:, 2]
    # Skin is the first thing to look wrong when saturation is pushed, and the
    # cube is a function of colour alone - so the protection can live right
    # here, in the table, instead of needing a mask.
    skin = np.exp(-(((chroma_b - 0.40) / 0.10) ** 2 + ((chroma_r - 0.60) / 0.08) ** 2))[:, None]
    amount = 1 + (params["saturation"] - 1) * (1 - 0.6 * skin)
    rgb = np.clip(luma + (rgb - luma) * amount, 0, 1.2)

    # Split-tone. The shadow and highlight weights are complementary windows on
    # luma that both fall away at the midtone, so the tone lands in the corners
    # of the picture and leaves faces alone.
    tone = float(params.get("tone", 0.0))
    if tone > 0.001:
        shadows = np.clip(1 - luma * 2.0, 0, 1) ** 1.5
        highlights = np.clip(luma * 2.0 - 1, 0, 1) ** 1.5
        # Teal in the shadows, a warm straw in the highlights; the magnitudes
        # are deliberately small, because a split-tone that can be named as a
        # colour has already gone too far.
        cool = np.array([-0.030, 0.004, 0.038], np.float32)
        warm = np.array([0.034, 0.010, -0.026], np.float32)
        rgb = rgb + tone * (shadows * cool + highlights * warm)
        # The tone shifts luminance a little; putting it back keeps exposure
        # where the black/white/gamma stage left it.
        toned = 0.2126 * rgb[:, 0] + 0.7152 * rgb[:, 1] + 0.0722 * rgb[:, 2]
        rgb = rgb + (luma[:, 0] - toned)[:, None] * 0.5
        rgb = np.clip(rgb, 0, 1.2)

    # A soft knee near white keeps a pushed highlight rolling off instead of
    # arriving at a hard clip.
    rgb = np.where(rgb > 0.94, 0.94 + (1 - 0.94) * np.tanh((rgb - 0.94) / (1 - 0.94)), rgb)
    rgb = np.clip(rgb, 0, 1)
    return np.clip(source + (rgb - source) * grade_strength(strength), 0, 1)


def grade_strength(amount: float) -> float:
    """Slider position to how much of the correction is actually applied.

    The slider is not a linear mix, because a linear one wastes its whole lower
    half: the first third of a colour correction is where nearly all of the
    visible change lives, and 50% of a mix that is itself gentle is nothing at
    all. This curve puts a usable grade in the middle of the travel and keeps
    the top end available for footage that really needs it.
    """
    amount = min(1.0, max(0.0, float(amount)))
    return float(amount ** 0.62)


def write_cube(path: Path, params: dict[str, float], strength: float,
               size: int = LUT_SIZE) -> None:
    """Write the grade as a .cube, the one file both FFmpeg and WebGL read."""
    values = grade_lattice(params, strength, size)
    lines = [
        "# Editoro automatic grade",
        f"# version: {GRADE_VERSION}",
        f"# measured: {json.dumps(params, sort_keys=True)}",
        f"# strength: {strength:.4f}",
        'TITLE "Editoro auto grade"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    lines += [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in values]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_cube(path: Path) -> dict[str, Any]:
    """Parse a .cube back into the flat RGB bytes the preview uploads.

    The preview samples the same table FFmpeg does, so a graded frame in the
    browser and a graded frame in the export are the same arithmetic rather
    than two things that resemble each other.
    """
    size = 0
    values: list[float] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("TITLE"):
            continue
        if line.upper().startswith("LUT_3D_SIZE"):
            size = int(line.split()[-1])
            continue
        if line.upper().startswith("DOMAIN"):
            continue
        parts = line.split()
        if len(parts) == 3:
            values.extend(float(part) for part in parts)
    if not size or len(values) != size ** 3 * 3:
        raise ValueError("that grade file is not a readable .cube")
    data = bytes(max(0, min(255, int(round(value * 255)))) for value in values)
    return {"size": size, "data": base64.b64encode(data).decode("ascii")}


# --------------------------------------------------------------- the job
class LookCanceled(Exception):
    pass


def _public_look(project: str, st: Optional[ProjectState] = None) -> dict[str, Any]:
    """Everything the panel and the agent need to know about the look."""
    if st is None:
        st = load_state(project)
    job = LOOK_JOBS.get(project)
    revision = st.source_revision
    payload: dict[str, Any] = {
        "project": project,
        "defocus": st.look.defocus,
        "grade": st.look.grade,
        "matte_ready": st.look.matte_revision == revision and matte_path(project).is_file(),
        "grade_ready": st.look.grade_revision == revision and cube_path(project).is_file(),
        "analysis": st.look.analysis,
        "model_ready": depth_model_ready(),
        "status": "idle",
        "progress": 0,
    }
    # A matte built by an older version is still a usable matte, so it keeps
    # working - but the panel says so, because the difference between the old
    # depth pass and this one is visible and nothing else would ever prompt a
    # rebuild.
    payload["matte_outdated"] = bool(
        payload["matte_ready"]
        and int(st.look.analysis.get("matte_version", 1)) < MATTE_VERSION)
    payload["matte_url"] = (
        f"/media/{project}/look/matte.mp4?v={st.look.matte_revision}"
        if payload["matte_ready"] else ""
    )
    if job:
        for key in ("status", "progress", "message", "error", "finished"):
            if key in job:
                payload[key] = job[key]
        payload["running"] = job.get("status") in {"queued", "model", "depth", "color"}
    else:
        payload["running"] = False
    return payload


async def _run_look_analysis(project: str, parts: set[str]) -> None:
    job = LOOK_JOBS[project]
    loop = asyncio.get_running_loop()

    def notify(**updates) -> None:
        job.update(updates)
        payload = {"type": "look_progress", **_public_look(project)}
        loop.call_soon_threadsafe(lambda: asyncio.create_task(HUB.broadcast(payload)))

    def work() -> dict[str, Any]:
        st = load_state(project)
        found: dict[str, Any] = {}
        if "depth" in parts:
            found.update(build_matte(project, st, job["cancel"], notify))
        if "color" in parts:
            notify(status="color", progress=0.90, message="Measuring the grade…")
            found.update(measure_grade(project, st))
        return found

    try:
        found = await asyncio.to_thread(work)
        if job["cancel"].is_set():
            raise LookCanceled()
        # Reload rather than reuse: the pass takes minutes, and the editor may
        # have moved half the timeline while it ran.
        st = load_state(project)
        if "depth" in parts:
            st.look.matte_revision = st.source_revision
            if st.look.defocus <= 0.001:
                st.look.defocus = 0.6
        if "color" in parts:
            st.look.grade_revision = st.source_revision
            if st.look.grade <= 0.001:
                st.look.grade = 0.7
            # The strength is baked into the table, so the table is written
            # last - once the strength that will actually be used is known.
            write_cube(cube_path(project), found, st.look.grade)
        st.look.analysis = {**st.look.analysis, **found}
        st = save_state(st)
        ensure_preview(project, st)
        notify(status="complete", progress=1.0, finished=time.time(),
               message="The look is ready.")
        await HUB.broadcast({"type": "state", "project": project,
                             "state": json.loads(st.model_dump_json())})
    except LookCanceled:
        notify(status="canceled", progress=0, message="Canceled.")
    except Exception as error:
        notify(status="error", progress=0, error=str(error),
               message=f"The look pass failed: {error}")
    finally:
        job["cancel"].set()


@app.get("/api/projects/{name}/look")
def api_look_status(name: str):
    require_project(name)
    return _public_look(name)


@app.get("/api/projects/{name}/look/lut")
def api_look_lut(name: str):
    require_project(name)
    ensure_cube(load_state(name))
    path = cube_path(name)
    if not path.is_file():
        raise HTTPException(404, "This project has no grade yet")
    try:
        return read_cube(path)
    except ValueError as error:
        raise HTTPException(500, str(error))


@app.post("/api/projects/{name}/look")
async def api_set_look(name: str, request: LookRequest):
    """Change the two amounts. Cheap: nothing is re-analysed here."""
    require_project(name)
    st = load_state(name)
    if request.defocus is not None:
        st.look.defocus = request.defocus
    if request.grade is not None:
        st.look.grade = request.grade
        # The strength lives inside the table, so changing it rewrites the
        # table. That is what keeps one file authoritative for both the
        # preview and the export.
        if st.look.analysis and cube_path(name).is_file():
            write_cube(cube_path(name), {
                key: value for key, value in st.look.analysis.items()
                if isinstance(value, (int, float))
            }, st.look.grade)
    save_state(st)
    # Debounced: a slider drag lands here on every mouse move, and the live
    # compositor is already showing the result while this waits.
    ensure_preview(name, st, delay=1.5)
    await HUB.broadcast({"type": "state", "project": name,
                         "state": json.loads(st.model_dump_json())})
    return _public_look(name, st)


@app.post("/api/projects/{name}/look/analyze")
async def api_analyze_look(name: str, req: Request):
    require_project(name)
    # Shape of the request first, state of the project second: a misspelled
    # part is wrong whether or not there is footage to run it on.
    body = await req.json() if await req.body() else {}
    parts = set(body.get("parts") or ["depth", "color"])
    if not parts <= {"depth", "color"}:
        raise HTTPException(400, "A look pass is made of 'depth' and 'color'")
    st = load_state(name)
    if not st.source:
        raise HTTPException(409, "Import source footage before building a look")
    current = LOOK_JOBS.get(name)
    if current and current.get("status") in {"queued", "model", "depth", "color"}:
        raise HTTPException(409, "A look pass is already running for this project")
    if name in EXPORT_CANCEL:
        raise HTTPException(409, "Wait for the export to finish before building a look")
    if "depth" in parts and importlib.util.find_spec("onnxruntime") is None:
        raise HTTPException(
            503,
            "The depth model runtime is not installed yet. Close Editoro and run launch.cmd once.",
        )
    LOOK_JOBS[name] = {
        "status": "queued", "progress": 0, "cancel": threading.Event(),
        "message": "Preparing…", "started": time.time(),
    }
    asyncio.create_task(_run_look_analysis(name, parts))
    return JSONResponse(_public_look(name, st), status_code=202)


@app.post("/api/projects/{name}/look/cancel")
async def api_cancel_look(name: str):
    require_project(name)
    job = LOOK_JOBS.get(name)
    if job and job.get("status") in {"queued", "model", "depth", "color"}:
        job["cancel"].set()
        job["status"] = "canceling"
        job["message"] = "Canceling…"
        await HUB.broadcast({"type": "look_progress", **_public_look(name)})
    return _public_look(name)


def _test_look() -> None:
    """The parts of the look that must be right before any footage is touched."""
    import numpy as np

    # The model input keeps the source aspect and stays on the 14-pixel grid
    # the network's patches require.
    for width, height in ((1920, 1080), (1080, 1920), (2160, 3840), (640, 640)):
        input_width, input_height = depth_input_size(width, height)
        assert input_width % 14 == 0 and input_height % 14 == 0, (width, height)
        assert max(input_width, input_height) == DEPTH_INPUT_EDGE
        assert abs(input_width / input_height - width / height) < 0.06, (width, height)
        matte_width, matte_height = matte_size(width, height)
        assert matte_width % 2 == 0 and matte_height % 2 == 0
        assert max(matte_width, matte_height) <= max(MATTE_EDGE, max(width, height))

    # One Amount has to mean one look: the radius is a share of the short edge,
    # so a 4K vertical frame gets a proportionally larger blur than 720p does.
    near_720, far_720 = defocus_sigmas(0.6, 1280, 720)
    near_4k, far_4k = defocus_sigmas(0.6, 2160, 3840)
    assert near_720 < far_720 and near_4k < far_4k
    assert abs(far_4k / far_720 - 2160 / 720) < 0.01
    assert defocus_sigmas(1.0, 1280, 720)[1] > far_720

    # A grade at zero strength has to be exactly identity - otherwise moving
    # the slider to 0 would still change the picture.
    params = {"gain_r": 0.94, "gain_b": 1.05, "black": 0.03, "white": 0.93,
              "gamma": 1.08, "contrast": 0.12, "saturation": 1.1}
    neutral = grade_lattice(params, 0.0, 17)
    axis = np.linspace(0, 1, 17, dtype=np.float32)
    blue, green, red = np.meshgrid(axis, axis, axis, indexing="ij")
    lattice = np.stack([red, green, blue], axis=-1).reshape(-1, 3)
    assert np.abs(neutral - lattice).max() < 1e-6

    graded = grade_lattice(params, 1.0, 17)
    assert graded.min() >= 0 and graded.max() <= 1
    assert np.abs(graded - lattice).max() > 0.01, "the grade did nothing at all"
    # The grade has to be visible. A correction whose average change is under
    # a percent is one the panel cannot be seen to do anything at all, which
    # is exactly what the first version of this shipped as.
    assert np.abs(graded - lattice).mean() > 0.02, "the grade is too subtle to see"
    # White stays white. The white balance is what would break this: applied
    # flat, a gain that neutralises a warm room also tints every highlight.
    assert np.abs(graded[-1] - 1.0).max() < 0.05, "the grade tints white"

    # Effectively monotonic along every axis. The highlight rolloff on the
    # white balance and the split-tone both read luma, so the table is not
    # monotonic to the last bit - but a step has to stay well inside one 8-bit
    # code, or a smooth gradient would come out of it in bands.
    cube = graded.reshape(17, 17, 17, 3)
    tolerance = 0.5 / 255
    assert (np.diff(cube[:, :, :, 2], axis=0) > -tolerance).all(), "blue folds back"
    assert (np.diff(cube[:, :, :, 1], axis=1) > -tolerance).all(), "green folds back"
    assert (np.diff(cube[:, :, :, 0], axis=2) > -tolerance).all(), "red folds back"

    # The slider is a curve, not a mix: half way along it does more than half
    # the correction, because the first part of a grade carries nearly all of
    # the visible change. It still has to be monotonic and hit both ends.
    assert grade_strength(0.0) == 0.0 and grade_strength(1.0) == 1.0
    positions = [grade_strength(x / 20) for x in range(21)]
    assert all(b >= a for a, b in zip(positions, positions[1:])), "the slider is not monotonic"
    assert grade_strength(0.5) > 0.55, "the middle of the slider does too little"
    half = grade_lattice(params, 0.5, 17)
    assert np.abs(half - (lattice + (graded - lattice) * grade_strength(0.5))).max() < 1e-6

    # The preview reads the same table FFmpeg does, through this round trip.
    with tempfile.TemporaryDirectory(prefix="editoro-lut-") as scratch:
        path = Path(scratch) / "grade.cube"
        write_cube(path, params, 0.8, 17)
        table = read_cube(path)
        assert table["size"] == 17
        decoded = base64.b64decode(table["data"])
        assert len(decoded) == 17 ** 3 * 3
        reference = grade_lattice(params, 0.8, 17)
        error = np.abs(np.frombuffer(decoded, np.uint8).reshape(-1, 3) / 255.0 - reference)
        assert error.max() < 0.01, error.max()

    # The filter graph. Off has to be genuinely off - the export's fast path
    # depends on an untouched project still being stream-copyable.
    name = f"look-selftest-{uuid.uuid4().hex[:8]}"
    folder = PROJECTS_DIR / name
    try:
        (folder / "look").mkdir(parents=True)
        # Breathing off, because this test is about the look: with it on there
        # is deliberately no such thing as an untouched frame, which is the
        # subject of _test_breathe() instead.
        state = ProjectState(name=name, source="source.mp4", source_revision="r1",
                             breathe="off")
        state.source_info = MediaInfo(width=1920, height=1080, fps=30, duration=10)
        segment = {"src_in": 0.0, "src_out": 1.0, "tl": 0.0, "camera": None}
        assert look_graph(state, "[0:v]", "[out]", 1) == ""
        assert look_inputs(state, 0, 1) == []
        assert not look_active(state)
        assert base_video_graph(state, segment, 30) == "[0:v]setpts=PTS-STARTPTS[base]"

        # Switched on but with nothing built yet is still off: a stale or
        # missing artefact must never be composited over the wrong pixels.
        state.look.defocus = 0.6
        state.look.grade = 0.7
        assert not look_active(state)
        matte_path(name).write_bytes(b"not really a video, but it is a file")
        write_cube(cube_path(name), params, 0.7, 9)
        state.look.matte_revision = "r0"
        state.look.grade_revision = "r0"
        assert not look_active(state), "a matte from the previous source was used"
        state.look.matte_revision = "r1"
        state.look.grade_revision = "r1"
        assert look_active(state) and look_state(state) == {"defocus": True, "grade": True}

        graph = look_graph(state, "[0:v]", "[looked]", 2)
        assert graph.startswith("[2:v]setpts"), graph[:40]
        assert "in_range=tv:out_range=pc" in graph
        assert graph.count("gblur=") == 2 and graph.count("alphamerge") == 2
        assert "lut3d=file=" in graph and graph.endswith("[looked]")
        assert look_inputs(state, 3.5, 2.0)[:2] == ["-ss", "3.500000"]
        # The camera move reads the defocused frame, not the raw one.
        assert base_video_graph(state, segment, 30, 2).endswith(
            "[looked]setpts=PTS-STARTPTS[base]")

        # Either half alone has to produce a valid single-ended chain.
        state.look.grade = 0.0
        assert look_graph(state, "[0:v]", "[looked]", 2).endswith(
            "overlay=0:0:format=auto[looked]")
        state.look.grade = 0.7
        state.look.defocus = 0.0
        only_grade = look_graph(state, "[0:v]", "[looked]", None)
        assert only_grade.startswith("[0:v]lut3d=") and only_grade.endswith("[looked]")
        assert look_inputs(state, 0, 1) == []
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("look tests OK")


# ------------------------------------------ [9e] The look on the GPU
# The defocus is two Gaussian blurs and a three-way composite. Done by FFmpeg's
# CPU filters at 4K those blurs are, by a wide margin, the slowest thing in an
# export - a hundred-pixel sigma is a hundred-pixel convolution over eight
# megapixels, twice per frame. The same work as an OpenCL kernel is a rounding
# error on any GPU made this decade.
#
# Two things make it cheap rather than merely parallel. The blurs run on a
# reduced copy chosen so the radius lands at a few pixels - a Gaussian sampled
# densely at low resolution is both faster and *better* than one sampled
# sparsely at full resolution, which is what produces the banded, ghosted edges
# a wide blur otherwise gets. And the composite samples those small slices with
# normalised coordinates, so the hardware's own bilinear unit does the upscale
# on the way in and there is no separate scaling pass at all.
#
# The grade stays on lut3d. It is a table lookup rather than a convolution, so
# it is not what an export waits for, and leaving it there keeps one .cube
# authoritative for the preview and the export both.
OPENCL_DIR = ROOT / ".opencl"   # generated kernels, rewritten per export
OPENCL_DEVICE: list[Optional[str]] = []
BLUR_WORKING_SIGMA = 6.0      # radius, in pixels, the blur is reduced to
BLUR_TAP_SIGMAS = 2.8         # how far out the kernel is sampled


def opencl_device() -> Optional[str]:
    """The `platform.device` index of a usable OpenCL GPU, or None.

    Machines routinely report more than one OpenCL platform - a real GPU and
    the integrated one - and FFmpeg refuses to guess between them. So the
    devices are enumerated, ordered with discrete GPUs first, and each is
    actually asked to run a kernel before being trusted.
    """
    if OPENCL_DEVICE:
        return OPENCL_DEVICE[0]
    OPENCL_DEVICE.append(None)
    listed = run([FFMPEG, "-hide_banner", "-v", "debug",
                  "-init_hw_device", "opencl=probe", "-f", "lavfi",
                  "-i", "nullsrc", "-frames:v", "0", "-f", "null", "-"])
    text = listed.stdout + listed.stderr
    # Lines look like: "0.0: NVIDIA CUDA / NVIDIA GeForce RTX 2060"
    found = re.findall(r"^\s*(?:\[[^\]]*\]\s*)?(\d+\.\d+):\s*(.+)$", text, re.M)
    if not found:
        return None

    def rank(entry: tuple[str, str]) -> int:
        name = entry[1].lower()
        if any(word in name for word in ("nvidia", "cuda", "radeon", "amd")):
            return 0
        if "intel" in name and "graphics" in name:
            return 2
        return 1

    for index, _name in sorted(found, key=rank):
        if opencl_device_works(index):
            OPENCL_DEVICE[0] = index
            return index
    return None


def opencl_device_works(index: str) -> bool:
    """Compile and run the real kernel shape on this device, once."""
    source = OPENCL_DIR / "probe.cl"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(kernel_source(4.0, 10.0, 0.01, (2.0, 2.0)), encoding="utf-8")
    probe = run([
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-init_hw_device", f"opencl=ocl:{index}", "-filter_hw_device", "ocl",
        "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=1:duration=1",
        "-filter_complex",
        "[0:v]format=rgba,split=4[a][b][c][d];"
        "[a]hwupload[full];[b]hwupload[m];[c]hwupload[n];[d]hwupload[f];"
        f"[full][m][n][f]program_opencl=source='{_filter_path(source)}'"
        ":kernel=look_mix:inputs=4[o];[o]hwdownload,format=rgba",
        "-frames:v", "1", "-f", "null", "-",
    ])
    return probe.returncode == 0


def gpu_look_available(st: ProjectState) -> bool:
    """Is the GPU path usable for this project's look right now?"""
    if os.environ.get("EDITORO_NO_GPU_LOOK"):
        return False
    return look_state(st)["defocus"] and opencl_device() is not None


def look_hw_args(st: ProjectState) -> list[str]:
    """Global FFmpeg arguments the GPU look needs, or none."""
    if not gpu_look_available(st):
        return []
    return ["-init_hw_device", f"opencl=ocl:{opencl_device()}",
            "-filter_hw_device", "ocl"]


def blur_taps(sigma: float) -> list[float]:
    """A normalised Gaussian, one tap per pixel, truncated where it vanishes."""
    radius = max(1, int(math.ceil(BLUR_TAP_SIGMAS * sigma)))
    weights = [math.exp(-(offset * offset) / (2 * sigma * sigma))
               for offset in range(radius + 1)]
    total = weights[0] + 2 * sum(weights[1:])
    return [weight / total for weight in weights]


def blur_scale(far_sigma: float) -> int:
    """How much to shrink the frame by before blurring it.

    Sized so the wider of the two radii comes out near BLUR_WORKING_SIGMA. The
    narrow slice is then a couple of pixels at the same scale, which is still
    plenty - detail finer than that is inside its own blur radius anyway.
    """
    return max(1, min(32, int(round(far_sigma / BLUR_WORKING_SIGMA))))


def _tap_array(name: str, weights: list[float]) -> str:
    body = ", ".join(f"{weight:.8f}f" for weight in weights)
    return (f"__constant int {name}_R = {len(weights) - 1};\n"
            f"__constant float {name}_W[{len(weights)}] = {{{body}}};\n")


def kernel_source(near_sigma: float, far_sigma: float, erode: float,
                  bands_scale: tuple[float, float]) -> str:
    """The whole look, as one OpenCL translation unit.

    The tap weights are compiled in rather than passed as arguments, because
    program_opencl has no way to hand a kernel anything but images - and a
    constant array the compiler can see unrolls better than one it cannot.
    """
    near = blur_taps(near_sigma)
    far = blur_taps(far_sigma)
    return f"""// Generated by Editoro. Do not edit; it is rewritten per export.
const sampler_t LIN = CLK_NORMALIZED_COORDS_TRUE | CLK_ADDRESS_CLAMP_TO_EDGE
                    | CLK_FILTER_LINEAR;
const sampler_t PT  = CLK_NORMALIZED_COORDS_FALSE | CLK_ADDRESS_CLAMP_TO_EDGE
                    | CLK_FILTER_NEAREST;

{_tap_array("NEAR", near)}{_tap_array("FAR", far)}
__constant float ERODE = {erode:.8f}f;
__constant int LUT_SIZE = {LUT_SIZE};
__constant float BAND_A = {DEFOCUS_NEAR_BAND[0]:.6f}f;
__constant float BAND_B = {DEFOCUS_NEAR_BAND[1]:.6f}f;
__constant float BAND_C = {DEFOCUS_FAR_BAND[0]:.6f}f;
__constant float BAND_D = {DEFOCUS_FAR_BAND[1]:.6f}f;

static float smoothband(float a, float b, float x) {{
  float t = clamp((x - a) / max(1e-4f, b - a), 0.f, 1.f);
  return t * t * (3.f - 2.f * t);
}}

static float2 centre(int2 p, int2 dim) {{
  return (convert_float2(p) + 0.5f) / convert_float2(dim);
}}

/* One axis of a Gaussian. The step is one pixel of the image being read, so
   the kernel is sampled at every pixel it covers rather than skipping across
   it - the frame has already been reduced to make that affordable. */
static float4 axis(__read_only image2d_t src, int2 p, int2 dim, float2 dir,
                   __constant float *weights, int radius) {{
  float2 uv = centre(p, dim);
  float2 step = dir / convert_float2(dim);
  float4 sum = read_imagef(src, LIN, uv) * weights[0];
  for (int i = 1; i <= radius; i++) {{
    float2 d = step * (float)i;
    sum += (read_imagef(src, LIN, uv + d) + read_imagef(src, LIN, uv - d)) * weights[i];
  }}
  return sum;
}}

#define PASS(name, weights, radius, dir)                                      \\
__kernel void name(__write_only image2d_t dst, unsigned int index,            \\
                   __read_only image2d_t src) {{                               \\
  int2 p = (int2)(get_global_id(0), get_global_id(1));                        \\
  int2 dim = get_image_dim(dst);                                              \\
  if (p.x >= dim.x || p.y >= dim.y) return;                                   \\
  write_imagef(dst, p, axis(src, p, dim, dir, weights, radius));              \\
}}

/* Weight every pixel by how much background it is, and carry that weight in
   alpha. Blurring the frame as it stands drags the subject's own bright edge
   out into the wall behind it, which reads as a halo traced around their hair
   and shoulders - the one thing that makes a defocus look fake. Blurring
   colour-times-weight and weight together, then dividing one by the other in
   the composite, means the background is blurred using only background: the
   subject contributes nothing to it at all. */
__kernel void premul(__write_only image2d_t dst, unsigned int index,
                     __read_only image2d_t video, __read_only image2d_t matte) {{
  int2 p = (int2)(get_global_id(0), get_global_id(1));
  int2 dim = get_image_dim(dst);
  if (p.x >= dim.x || p.y >= dim.y) return;
  float2 uv = centre(p, dim);
  float d = read_imagef(matte, LIN, uv).x;
  d = min(d, read_imagef(matte, LIN, uv + (float2)(ERODE, 0.f)).x);
  d = min(d, read_imagef(matte, LIN, uv - (float2)(ERODE, 0.f)).x);
  d = min(d, read_imagef(matte, LIN, uv + (float2)(0.f, ERODE)).x);
  d = min(d, read_imagef(matte, LIN, uv - (float2)(0.f, ERODE)).x);
  float w = smoothband(BAND_A, BAND_B, d);
  float4 c = read_imagef(video, PT, p);
  write_imagef(dst, p, (float4)(c.xyz * w, w));
}}

PASS(near_h, NEAR_W, NEAR_R, (float2)(1.f, 0.f))
PASS(near_v, NEAR_W, NEAR_R, (float2)(0.f, 1.f))
PASS(far_h,  FAR_W,  FAR_R,  (float2)(1.f, 0.f))
PASS(far_v,  FAR_W,  FAR_R,  (float2)(0.f, 1.f))

/* Undo the weighting the blur was done under. Where almost no background
   reached a pixel there is nothing to recover, so it falls back to the sharp
   frame rather than dividing by nearly zero and inventing a colour. */
static float4 unweight(float4 blurred, float4 sharp) {{
  float w = blurred.w;
  return w > 0.004f ? (float4)(blurred.xyz / w, 1.f) : sharp;
}}

/* The grade, sampled out of the table strip. The hardware interpolates red
   and green inside a tile; blue is a lerp between two tiles, done here because
   they are neighbours in x rather than in a third dimension. */
static float4 grade(__read_only image2d_t lut, float4 c) {{
  float n = (float)LUT_SIZE;
  float3 v = clamp(c.xyz, 0.f, 1.f) * ((n - 1.f) / n) + 0.5f / n;
  float b = clamp(c.z, 0.f, 1.f) * (n - 1.f);
  float slice = floor(b);
  float frac = b - slice;
  float tile = 1.f / n;
  float2 base = (float2)(v.x * tile, v.y);
  float4 lo = read_imagef(lut, LIN, base + (float2)(slice * tile, 0.f));
  float4 hi = read_imagef(lut, LIN, base + (float2)(min(slice + 1.f, n - 1.f) * tile, 0.f));
  float4 out = mix(lo, hi, frac);
  out.w = c.w;
  return out;
}}

/* Three depth slices - sharp, softened, thrown away - blended by the matte.
   `near` and `far` are the reduced blurs; sampling them with normalised
   coordinates and a linear filter is what upscales them, so nothing in the
   graph has to scale them back up first. */
__kernel void look_mix(__write_only image2d_t dst, unsigned int index,
                       __read_only image2d_t video,
                       __read_only image2d_t matte,
                       __read_only image2d_t near,
                       __read_only image2d_t far) {{
  int2 p = (int2)(get_global_id(0), get_global_id(1));
  int2 dim = get_image_dim(dst);
  if (p.x >= dim.x || p.y >= dim.y) return;
  float2 uv = centre(p, dim);
  float4 c = read_imagef(video, PT, p);

  /* The matte as it is, with no erosion. Eroding here would push the blur a
     few pixels away from the subject and leave a rim of sharp background
     tracing their outline, which reads as a cutout. The erosion belongs on
     the weighting instead, where it keeps the subject out of the blur without
     moving where the blur is shown. */
  float d = read_imagef(matte, LIN, uv).x;

  float4 result = mix(c, unweight(read_imagef(near, LIN, uv), c),
                      smoothband(BAND_A, BAND_B, d));
  result = mix(result, unweight(read_imagef(far, LIN, uv), c),
               smoothband(BAND_C, BAND_D, d));
  result.w = 1.f;
  write_imagef(dst, p, result);
}}

/* The same thing with the grade folded in. It is a separate kernel rather than
   a branch because program_opencl fixes the number of inputs at graph time,
   and a project with no grade should not have to feed it a table. */
__kernel void look_mix_graded(__write_only image2d_t dst, unsigned int index,
                              __read_only image2d_t video,
                              __read_only image2d_t matte,
                              __read_only image2d_t near,
                              __read_only image2d_t far,
                              __read_only image2d_t lut) {{
  int2 p = (int2)(get_global_id(0), get_global_id(1));
  int2 dim = get_image_dim(dst);
  if (p.x >= dim.x || p.y >= dim.y) return;
  float2 uv = centre(p, dim);
  float4 c = read_imagef(video, PT, p);

  float d = read_imagef(matte, LIN, uv).x;   // no erosion here; see look_mix

  float4 result = mix(c, unweight(read_imagef(near, LIN, uv), c),
                      smoothband(BAND_A, BAND_B, d));
  result = mix(result, unweight(read_imagef(far, LIN, uv), c),
               smoothband(BAND_C, BAND_D, d));
  result = grade(lut, result);
  result.w = 1.f;
  write_imagef(dst, p, result);
}}
"""


def write_look_kernel(st: ProjectState, pixel_scale: float = 1.0) -> Path:
    """Write this project's kernel and return its path.

    `pixel_scale` is how big the frame being processed is next to the master.
    The preview proxy runs the same look at a fraction of the size, and a blur
    radius is measured in pixels, so it has to come along for the ride.
    """
    width = max(2, int(st.source_info.width * pixel_scale))
    height = max(2, int(st.source_info.height * pixel_scale))
    near, far = defocus_sigmas(st.look.defocus, width, height)
    scale = blur_scale(far)
    matte_width, _ = matte_size(st.source_info.width, st.source_info.height)
    path = OPENCL_DIR / f"look-{st.name}-{round(pixel_scale * 1000)}.cl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(kernel_source(
        max(0.6, near / scale), max(0.8, far / scale),
        # The erosion is three pixels of the stored matte, restated as a
        # fraction of the frame so the kernel does not need to know any sizes.
        3.0 / max(2, matte_width),
        (float(scale), float(scale)),
    ), encoding="utf-8")
    return path


def lut_image_path(project: str) -> Path:
    return look_dir(project) / "grade.png"


def write_lut_image(st: ProjectState) -> Path:
    """The grade table as one wide image, so the kernel can sample it.

    A 33-cube laid out as 33 tiles of 33x33 side by side. OpenCL has 3D images,
    but FFmpeg can only hand a kernel things that arrived as video frames, and
    a strip of tiles is what a video frame can carry. The kernel interpolates
    between two tiles by hand; within a tile the hardware does it.
    """
    path = lut_image_path(st.name)
    cube = cube_path(st.name)
    if (path.is_file() and cube.is_file()
            and path.stat().st_mtime >= cube.stat().st_mtime):
        return path
    table = read_cube(cube)
    size = table["size"]
    data = base64.b64decode(table["data"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # Row-major within a tile, tiles laid out along blue. The .cube varies red
    # fastest, which is already this order.
    row = bytearray(size * size * 3)
    strip = bytearray()
    for y in range(size):
        for blue in range(size):
            start = (blue * size * size + y * size) * 3
            strip += data[start:start + size * 3]
    partial = path.with_suffix(".part.png")
    process = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{size * size}x{size}", "-i", "pipe:0",
         "-frames:v", "1", str(partial)],
        input=bytes(strip), capture_output=True, creationflags=CHILD_FLAGS)
    if process.returncode:
        raise RuntimeError(process.stderr.decode("utf-8", "replace")[-300:])
    os.replace(partial, path)
    return path


def look_graph_gpu(st: ProjectState, input_label: str, output_label: str,
                   matte_index: int, pixel_scale: float = 1.0) -> str:
    """The defocus on the GPU, from `input_label` to `output_label`."""
    width = max(2, int(st.source_info.width * pixel_scale))
    height = max(2, int(st.source_info.height * pixel_scale))
    _near, far = defocus_sigmas(st.look.defocus, width, height)
    scale = blur_scale(far)
    blur_width = max(8, int(round(width / scale / 2)) * 2)
    blur_height = max(8, int(round(height / scale / 2)) * 2)
    kernel = _filter_path(write_look_kernel(st, pixel_scale))
    program = f"program_opencl=source='{kernel}':kernel="
    parts = [
        # RGBA rather than the source's yuv420p: a planar format would hand the
        # kernel the matte's *chroma* plane when it asks for the matte during a
        # chroma pass, and the blur is a straight per-channel average anyway.
        f"{input_label}setpts=PTS-STARTPTS,format=rgba,split=2[lkfull][lksmall]",
        f"[lksmall]scale={blur_width}:{blur_height}:flags=area,hwupload[lkq]",
        "[lkfull]hwupload[lkbig]",
        # The matte goes in at the blur's resolution: the kernel samples it
        # with normalised coordinates, so it never needs to match anything.
        f"[{matte_index}:v]setpts=PTS-STARTPTS,"
        f"scale={blur_width}:{blur_height}:flags=bicubic:in_range=tv:out_range=pc,"
        "format=rgba,tpad=stop=-1:stop_mode=clone,hwupload,split=2[lkm1][lkm2]",
        # Weight by background-ness before blurring, not after.
        f"[lkq][lkm1]{program}premul:inputs=2[lkw]",
        "[lkw]split=2[lkq1][lkq2]",
        f"[lkq1]{program}near_h[lkna]",
        f"[lkna]{program}near_v[lknear]",
        f"[lkq2]{program}far_h[lkfa]",
        f"[lkfa]{program}far_v[lkfar]",
    ]
    if look_state(st)["grade"]:
        # The grade rides along in the same kernel. Sent down the CPU chain as
        # lut3d it was, on 4K footage, about a third of what was left of the
        # export - and it is a table lookup, which is the one thing a GPU is
        # even better at than a blur.
        parts.append(f"[{matte_index + 1}:v]format=rgba,hwupload[lklut]")
        parts.append(
            f"[lkbig][lkm2][lknear][lkfar][lklut]{program}look_mix_graded:inputs=5[lkgpu]")
    else:
        parts.append(f"[lkbig][lkm2][lknear][lkfar]{program}look_mix:inputs=4[lkgpu]")
    parts.append(f"[lkgpu]hwdownload,format=rgba,format=yuv420p{output_label}")
    return ";".join(parts)


# ------------------------------------------ [9d] Preview proxies
# The editor does not play the master. A talking-head master is routinely 4K,
# H.264 with a two-second GOP, and several gigabytes; a browser asked to scrub
# that spends its whole frame budget decoding. So the source is transcoded once
# into a small, short-GOP copy that seeks instantly, and - when a look is on -
# a second copy with the look already burned into it, so playback costs one
# video decode instead of two plus a shader chain. Neither file is ever read by
# an export.
PREVIEW_EDGE = 960            # long edge of a proxy
PREVIEW_GOP = 12              # frames between keyframes; short enough to scrub
PREVIEW_JOBS: dict[str, dict[str, Any]] = {}


def preview_dir(project: str) -> Path:
    return project_dir(project) / "preview"


def preview_base_path(project: str) -> Path:
    return preview_dir(project) / "base.mp4"


def preview_look_path(project: str) -> Path:
    return preview_dir(project) / "look.mp4"


def preview_base_key(st: ProjectState) -> str:
    """What the plain proxy depends on: the footage, and nothing else."""
    return f"{st.source}:{st.source_revision}" if st.source else ""


def preview_look_key(st: ProjectState) -> str:
    """What the baked proxy depends on: the footage and both look amounts.

    The matte revision is in here too, because re-running the depth pass
    changes the pixels the same way moving the slider does.
    """
    flags = look_state(st)
    if not (flags["defocus"] or flags["grade"]):
        return ""
    return ":".join([
        preview_base_key(st),
        f"d{st.look.defocus:.4f}" if flags["defocus"] else "d0",
        f"g{st.look.grade:.4f}" if flags["grade"] else "g0",
        st.look.matte_revision, str(GRADE_VERSION), str(DEFOCUS_VERSION),
    ])


def preview_size(width: int, height: int) -> tuple[int, int]:
    scale = min(1.0, PREVIEW_EDGE / max(1, max(width, height)))
    return (max(2, int(round(width * scale / 2)) * 2),
            max(2, int(round(height * scale / 2)) * 2))


def preview_encoder() -> list[str]:
    """NVENC when the machine has it, x264 when it does not.

    A proxy is a throwaway file whose only job is to decode fast, so this
    leans on speed and a short GOP rather than on efficiency.
    """
    if nvenc_available():
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
                "-rc", "vbr", "-cq", "26", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-tune", "fastdecode"]


def _short_key(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _public_preview(project: str, st: Optional[ProjectState] = None) -> dict[str, Any]:
    if st is None:
        st = load_state(project)
    job = PREVIEW_JOBS.get(project)
    base_ready = (st.preview.base_key == preview_base_key(st)
                  and bool(st.source) and preview_base_path(project).is_file())
    look_key = preview_look_key(st)
    look_ready = (bool(look_key) and st.preview.look_key == look_key
                  and preview_look_path(project).is_file())
    payload: dict[str, Any] = {
        "project": project,
        "base_ready": base_ready,
        "look_ready": look_ready,
        # What the browser should actually play, in one field, so the client
        # never has to reason about which files exist.
        "url": "",
        "baked": look_ready,
        "status": "idle",
        "progress": 0,
        "running": False,
    }
    if look_ready:
        payload["url"] = f"/media/{project}/preview/look.mp4?v={_short_key(look_key)}"
    elif base_ready:
        payload["url"] = f"/media/{project}/preview/base.mp4?v={_short_key(st.preview.base_key)}"
    elif st.source:
        payload["url"] = f"/media/{project}/{st.source}?v={st.source_revision}"
    if job:
        for key in ("status", "progress", "message", "error"):
            if key in job:
                payload[key] = job[key]
        payload["running"] = job.get("status") in {"queued", "base", "look"}
    return payload


def _run_preview_ffmpeg(args: list[str], target: Path, total_frames: int,
                        cancel: threading.Event, report) -> None:
    """Run one proxy transcode, reporting progress and honouring cancel."""
    partial = target.with_name(target.stem + ".part.mp4")
    partial.unlink(missing_ok=True)
    process = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-progress", "pipe:1", "-stats_period", "0.4", *args, str(partial)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=CHILD_FLAGS)
    try:
        for line in process.stdout:
            if cancel.is_set():
                raise LookCanceled()
            if line.startswith("frame=") and total_frames > 0:
                try:
                    report(min(1.0, int(line.split("=", 1)[1]) / total_frames))
                except ValueError:
                    pass
        if process.wait():
            detail = (process.stderr.read() or "")[-400:]
            raise RuntimeError(f"the proxy could not be built: {detail}")
    except BaseException:
        if process.poll() is None:
            process.terminate()
        process.wait()
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, target)


def build_preview_base(project: str, st: ProjectState, cancel: threading.Event, report) -> None:
    source = project_dir(project) / st.source
    width, height = preview_size(st.source_info.width, st.source_info.height)
    preview_dir(project).mkdir(parents=True, exist_ok=True)
    total = int((st.source_info.duration or 0) * (st.source_info.fps or 30))
    _run_preview_ffmpeg([
        "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale={width}:{height}:flags=bicubic,format=yuv420p",
        *preview_encoder(), "-g", str(PREVIEW_GOP), "-bf", "0",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
    ], preview_base_path(project), total, cancel, report)


def build_preview_look(project: str, st: ProjectState, cancel: threading.Event, report) -> None:
    """The same proxy with the look already in it.

    This is the file that makes playback cheap: the browser decodes one small
    video and draws it, with no matte to decode alongside it and no shader to
    run per frame.

    It is built from the plain proxy rather than from the master, and that is
    the whole reason it is usable. Applying a look to 4K footage runs at about
    ten frames a second; applying the same look to a 540-pixel copy of it runs
    faster than real time, which is the difference between waiting a quarter of
    an hour after moving a slider and waiting under a minute. The blur radius
    is scaled to match, so the proxy shows the same look the export will make -
    at the resolution it will be watched at here.
    """
    ensure_cube(st)
    base = preview_base_path(project)
    if not base.is_file():
        raise RuntimeError("the plain preview copy has to exist first")
    width, height = preview_size(st.source_info.width, st.source_info.height)
    scale = width / max(1, st.source_info.width)
    total = int((st.source_info.duration or 0) * (st.source_info.fps or 30))
    flags = look_state(st)
    inputs = ["-i", str(base)]
    matte_index = None
    if flags["defocus"]:
        inputs += ["-i", str(matte_path(project))]
        matte_index = 1
    if matte_index is not None and gpu_look_available(st):
        if flags["grade"]:
            # The kernel reads the grade out of an image, so the image has to
            # be an input - looped for the length of the file, like the export
            # does it for the length of a segment.
            inputs += ["-loop", "1", "-framerate", f"{st.source_info.fps or 30:.6f}",
                       "-i", str(write_lut_image(st))]
        graph = look_graph_gpu(st, "[0:v]", "[looked]", matte_index, scale)
    else:
        graph = look_graph(st, "[0:v]", "[looked]", matte_index, scale)
    chain = f"{graph};[looked]" if graph else "[0:v]"
    _run_preview_ffmpeg([
        *look_hw_args(st), *inputs,
        "-filter_complex", f"{chain}format=yuv420p[pv]",
        "-map", "[pv]", "-map", "0:a:0?",
        *preview_encoder(), "-g", str(PREVIEW_GOP), "-bf", "0",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart", "-shortest",
    ], preview_look_path(project), total, cancel, report)


async def _run_preview_job(project: str, parts: list[str]) -> None:
    job = PREVIEW_JOBS[project]
    loop = asyncio.get_running_loop()

    def notify(**updates) -> None:
        job.update(updates)
        payload = {"type": "preview_progress", **_public_preview(project)}
        loop.call_soon_threadsafe(lambda: asyncio.create_task(HUB.broadcast(payload)))

    def work() -> dict[str, str]:
        st = load_state(project)
        done: dict[str, str] = {}
        if "base" in parts:
            notify(status="base", progress=0.0, message="Building the preview copy...")
            build_preview_base(project, st, job["cancel"], lambda fraction: notify(
                status="base", progress=fraction,
                message=f"Building the preview copy... {round(fraction * 100)}%"))
            done["base_key"] = preview_base_key(st)
        if "look" in parts and preview_look_key(st) and preview_base_path(project).is_file():
            notify(status="look", progress=0.0, message="Baking the look into the preview...")
            build_preview_look(project, st, job["cancel"], lambda fraction: notify(
                status="look", progress=fraction,
                message=f"Baking the look into the preview... {round(fraction * 100)}%"))
            done["look_key"] = preview_look_key(st)
        return done

    try:
        done = await asyncio.to_thread(work)
        if job["cancel"].is_set():
            raise LookCanceled()
        st = load_state(project)
        for key, value in done.items():
            setattr(st.preview, key, value)
        st = save_state(st)
        notify(status="complete", progress=1.0, message="")
        await HUB.broadcast({"type": "state", "project": project,
                             "state": json.loads(st.model_dump_json())})
    except LookCanceled:
        notify(status="canceled", progress=0, message="")
    except Exception as error:
        notify(status="error", progress=0, error=str(error),
               message=f"The preview copy failed: {error}")
    finally:
        job["cancel"].set()


def preview_wanted(st: ProjectState) -> list[str]:
    """Which proxies are missing or stale for this project right now."""
    if not st.source:
        return []
    parts = []
    if (st.preview.base_key != preview_base_key(st)
            or not preview_base_path(st.name).is_file()):
        parts.append("base")
    key = preview_look_key(st)
    if key and (st.preview.look_key != key or not preview_look_path(st.name).is_file()):
        parts.append("look")
    return parts


def ensure_preview(project: str, st: Optional[ProjectState] = None,
                   delay: float = 0.0) -> None:
    """Start a proxy build if one is needed, replacing any build in flight.

    Dragging a slider changes what the baked proxy should contain on every
    mouse move, so the build is debounced: a new request cancels the running
    one and waits out the delay before starting, and the live compositor covers
    the preview in the meantime.
    """
    if st is None:
        st = load_state(project)
    parts = preview_wanted(st)
    if not parts:
        return
    running = PREVIEW_JOBS.get(project)
    if running:
        if not running["cancel"].is_set() and running.get("parts") == parts:
            return
        running["cancel"].set()
    cancel = threading.Event()
    PREVIEW_JOBS[project] = {"status": "queued", "progress": 0, "cancel": cancel,
                             "parts": parts, "message": "Preparing the preview copy..."}

    async def start() -> None:
        if delay:
            await asyncio.sleep(delay)
        if cancel.is_set() or PREVIEW_JOBS.get(project, {}).get("cancel") is not cancel:
            return
        await _run_preview_job(project, parts)

    asyncio.create_task(start())


@app.get("/api/projects/{name}/preview")
def api_preview_status(name: str):
    require_project(name)
    return _public_preview(name)


@app.post("/api/projects/{name}/preview/build")
async def api_preview_build(name: str):
    require_project(name)
    st = load_state(name)
    if not st.source:
        raise HTTPException(409, "Import source footage before building a preview copy")
    ensure_preview(name, st)
    return JSONResponse(_public_preview(name, st), status_code=202)


# ------------------------------------------- [10] Export — smart rendering
NVENC_CACHE: list[bool] = []


def nvenc_available() -> bool:
    """has_nvenc(), asked once. The probe spawns FFmpeg, so it is not free."""
    if not NVENC_CACHE:
        try:
            NVENC_CACHE.append(has_nvenc())
        except Exception:
            NVENC_CACHE.append(False)
    return NVENC_CACHE[0]


def has_nvenc() -> bool:
    listed = run([FFMPEG, "-hide_banner", "-encoders"])
    if "h264_nvenc" not in listed.stdout:
        return False
    probe_encode = run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        # Turing NVENC rejects frames below its minimum supported dimension.
        "-i", "color=black:s=256x256:r=1", "-frames:v", "1",
        "-c:v", "h264_nvenc", "-f", "null", "-"
    ])
    return probe_encode.returncode == 0


def count_frames(path: Path) -> int:
    """Exact decoded frame count. Container duration is not a reliable proxy."""
    for flag, entry in (("-count_packets", "nb_read_packets"),
                        ("-count_frames", "nb_read_frames")):
        result = run([
            FFPROBE, "-v", "error", "-select_streams", "v:0", flag,
            "-show_entries", f"stream={entry}", "-of", "csv=p=0", str(path),
        ])
        try:
            counted = int((result.stdout or "0").strip().split(",")[0] or 0)
        except ValueError:
            counted = 0
        if counted:
            return counted
    return 0


def keyframes(src: Path, log) -> list[float]:
    p = run([FFPROBE, "-v", "quiet", "-select_streams", "v", "-show_entries",
             "packet=pts_time,flags", "-of", "csv=p=0", str(src)], log)
    frames = []
    for row in p.stdout.splitlines():
        columns = row.split(",")
        try:
            if len(columns) >= 2 and "K" in columns[-1]:
                frames.append(float(columns[0]))
        except ValueError:
            continue
    return frames or [0.0]


def camera_effect_at(st: ProjectState, timeline_time: float) -> Optional[Instance]:
    cameras = camera_packs()
    active = [
        item for item in st.instances
        if item.template in cameras
        and isinstance(item.fields.get("rect"), dict)
        and item.start <= timeline_time < item.start + item.duration
    ]
    return min(active, key=lambda item: (item.track, item.start, item.id), default=None)


# A clean span shorter than this is folded into its rendered neighbour instead
# of being stream-copied on its own.
GAP_ABSORB_SECONDS = 0.30
GAP_ABSORB_FRAMES = 10


def timeline_duration(st: ProjectState) -> float:
    """Length of the edit, which is the sum of the kept source spans."""
    kept = sum(cut.src_out - cut.src_in for cut in st.cuts)
    return round(kept or st.source_info.duration or 0.0, 4)


def timeline_to_source(st: ProjectState, timeline_time: float) -> float:
    """Timeline seconds -> source seconds, walking the kept cuts."""
    elapsed = 0.0
    for cut in st.cuts:
        length = cut.src_out - cut.src_in
        if timeline_time < elapsed + length:
            return cut.src_in + (timeline_time - elapsed)
        elapsed += length
    if st.cuts:
        return st.cuts[-1].src_out
    return max(0.0, min(timeline_time, st.source_info.duration))


def timeline_segments(st: ProjectState) -> list[dict]:
    """Resolve cuts → ordered (src_in, src_out, tl_start) list, then classify
    clean/dirty by intersection with instances (incl. punch-in) & captions."""
    segs, t = [], 0.0
    cuts = st.cuts or ([Cut(src_in=0, src_out=st.source_info.duration)] if st.source_info.duration else [])
    for c in cuts:
        segs.append({"src_in": c.src_in, "src_out": c.src_out, "tl": t})
        t += c.src_out - c.src_in
    # spans (timeline time) that make a region dirty
    spans = [(i.start, i.start + i.duration) for i in st.instances] + \
            [(b.start, b.end) for b in st.captions]
    out = []
    frame = 1 / max(1.0, st.source_info.fps or 30)
    breathing = breathes(st)

    def on_frame(value: float) -> float:
        return math.floor(value / frame + 0.5) * frame

    for s in segs:
        length = s["src_out"] - s["src_in"]
        raw_marks = (
            {0.0, length}
            | {max(0, min(length, a - s["tl"])) for a, _ in spans}
            | {max(0, min(length, b - s["tl"])) for _, b in spans}
        )
        marks = sorted({
            0.0 if value <= frame / 2 else
            length if value >= length - frame / 2 else
            max(0, min(length, on_frame(value)))
            for value in raw_marks
        })
        for a, b in zip(marks, marks[1:]):
            if b - a < frame / 2:
                continue
            mid = s["tl"] + (a + b) / 2
            # Breathing moves the footage everywhere at once, so with it on
            # there is no such thing as a segment that can be stream-copied.
            dirty = breathing or any(x <= mid < y for x, y in spans)
            camera = camera_effect_at(st, mid)
            out.append({"src_in": s["src_in"] + a, "src_out": s["src_in"] + b,
                        "tl": s["tl"] + a, "dirty": dirty,
                        "camera": camera.id if camera else None})
    # A short clean gap between two overlays is not worth its own FFmpeg process.
    # Copying two frames saves nothing, and every extra span is another join to
    # get exactly right; absorbing those gaps into the neighbouring rendered span
    # turns a densely decorated minute from fifty processes into a handful.
    minimum_clean = max(GAP_ABSORB_SECONDS, frame * GAP_ABSORB_FRAMES)
    for index, segment in enumerate(out):
        if segment["dirty"]:
            continue
        if segment["src_out"] - segment["src_in"] >= minimum_clean:
            continue
        before = out[index - 1]["dirty"] if index else False
        after = out[index + 1]["dirty"] if index + 1 < len(out) else False
        if before or after:
            segment["dirty"] = True
            segment["camera"] = (
                out[index - 1].get("camera") if before else out[index + 1].get("camera")
            )

    # Renderer state can change inside a dirty span, so adjacent overlay/caption
    # intervals do not need separate FFmpeg processes. Camera intervals remain
    # separate because their source transform is expressed by FFmpeg.
    merged: list[dict] = []
    for segment in out:
        previous = merged[-1] if merged else None
        contiguous = previous and (
            abs(previous["src_out"] - segment["src_in"]) <= frame / 2
            and abs(
                previous["tl"] + previous["src_out"] - previous["src_in"]
                - segment["tl"]
            ) <= frame / 2
        )
        if (
            contiguous
            and previous["dirty"] == segment["dirty"]
            and previous.get("camera") == segment.get("camera")
        ):
            previous["src_out"] = segment["src_out"]
        else:
            merged.append(segment.copy())
    return merged


def video_encoder(st: ProjectState, nvenc: bool) -> list[str]:
    """A high-quality encoder configured to match the source.

    Smart-lossless export interleaves stream-copied source spans with spans we
    re-encode. Those two kinds of span end up in one file, so the encoder has to
    agree with the source on pixel format and profile - otherwise the joins are
    where playback stutters or a decoder gives up partway through.
    """
    hevc = st.source_info.codec in {"hevc", "h265"}
    pix_fmt = st.source_info.pix_fmt if st.source_info.pix_fmt in {
        "yuv420p", "yuvj420p", "yuv420p10le", "nv12",
    } else "yuv420p"
    if pix_fmt in {"yuvj420p", "nv12"}:
        pix_fmt = "yuv420p"
    profile = (st.source_info.profile or "").lower()
    if nvenc:
        codec = "hevc_nvenc" if hevc else "h264_nvenc"
        args = [
            "-c:v", codec, "-preset", "p5", "-tune", "hq",
            "-rc", "constqp", "-qp", "16", "-spatial_aq", "1", "-aq-strength", "8",
            "-pix_fmt", pix_fmt,
        ]
        if not hevc and profile in {"baseline", "main", "high"}:
            args += ["-profile:v", profile]
        return args
    codec = "libx265" if hevc else "libx264"
    args = ["-c:v", codec, "-preset", "faster", "-crf", "16", "-pix_fmt", pix_fmt]
    if not hevc and profile in {"baseline", "main", "high"}:
        args += ["-profile:v", profile]
    return args


def bitstream_filter(st: ProjectState) -> list[str]:
    """Annex-B conversion, needed to put a span into an MPEG-TS join stream."""
    if st.source_info.codec in {"hevc", "h265"}:
        return ["-bsf:v", "hevc_mp4toannexb"]
    return ["-bsf:v", "h264_mp4toannexb"]


def segment_frames(seg: dict, fps: float) -> int:
    """Frames this span must contribute, so the joined video cannot drift."""
    planned = seg.get("frames")
    if isinstance(planned, int) and planned > 0:
        return planned
    start = float(seg.get("tl", 0.0))
    end = start + (seg["src_out"] - seg["src_in"])
    return max(1, int(round(end * fps)) - int(round(start * fps)))


def plan_segment_frames(segments: list[dict], fps: float) -> int:
    """Assign each span an exact frame count and return the total.

    Rounding each span's own length independently lets the errors accumulate:
    across fifty spans that is a couple of frames of drift against the audio,
    which is the class of bug you only notice at the end of a finished video.
    Walking one accumulator makes the counts telescope, so the sum is exactly
    the length of the edit by construction.
    """
    elapsed = 0.0
    previous_frame = 0
    for segment in segments:
        elapsed += segment["src_out"] - segment["src_in"]
        boundary = int(round(elapsed * fps))
        segment["frames"] = max(1, boundary - previous_frame)
        previous_frame = previous_frame + segment["frames"]
    return previous_frame


def _ease_expr(kind: str, k: str) -> str:
    """FFmpeg-expression easing that matches the JavaScript engine exactly."""
    if kind == "outCubic":
        return f"(1-pow(1-{k},3))"
    if kind == "inCubic":
        return f"pow({k},3)"
    if kind == "outQuint":
        return f"(1-pow(1-{k},5))"
    if kind == "inOutCubic":
        return (f"if(lt({k},0.5),4*pow({k},3),1-pow(-2*{k}+2,3)/2)")
    if kind == "outExpo":
        return f"(1-pow(2,-10*{k}))"
    return f"(1-pow(1-{k},3))"


# ------------------------------------------------------------ breathing zoom
# One slow scale oscillation running under the whole project, so no shot is
# ever completely still. The numbers are small on purpose: at "standard" the
# frame travels 2.4% over roughly thirteen seconds, which is felt rather than
# seen. Vertical carries more amplitude because a 9:16 frame is physically
# smaller in a feed, so the same percentage reads as less movement.
#
# The tempo is one constant across all three strengths, and that is the whole
# design: every overlay's idle drift locks to this same clock, so turning the
# strength up makes the video breathe deeper, never faster. A second control
# for speed would let the footage and the graphics fall out of step, which is
# exactly the thing this replaces.
BREATHE_HZ = 0.12
# Amplitude is half the peak-to-peak zoom: `standard` horizontal travels 4.4% of
# the frame across each breath. The first numbers here were a quarter of these
# and the effect was, correctly, reported as doing nothing at all - a 1.2% zoom
# spread over thirteen seconds is below the threshold at which a frame reads as
# alive rather than as a still. Raise these two tables together with the copy in
# index.html or the preview and the export stop being the same picture.
BREATHE_LEVELS: dict[str, dict[str, float]] = {
    "off": {"horizontal": 0.0, "vertical": 0.0},
    "subtle": {"horizontal": 0.012, "vertical": 0.018},
    "standard": {"horizontal": 0.022, "vertical": 0.032},
    "strong": {"horizontal": 0.038, "vertical": 0.052},
}
# How long an override takes to reach its own amplitude. Amplitude is a zoom,
# so stepping it would be a visible jump in the frame; a third of a second of
# linear ramp turns every edge into something nobody can point at.
BREATHE_RAMP = 0.4


def breathe_level(level: str, orientation: str) -> float:
    table = BREATHE_LEVELS.get(level, BREATHE_LEVELS["off"])
    return table.get(orientation, table["horizontal"])


def breathe_framing(st: ProjectState, item: Instance) -> tuple[float, float, float]:
    """One breathe block's depth and the point it pulls towards.

    Mirrors breatheFraming() in index.html. The block's rectangle is a limit on
    top of its strength rather than a replacement for it: the breath pulls at
    the middle of the box and is not allowed to crop past its edges, so a block
    left at the default box breathes exactly as it did when the box did not
    exist. The zoom is uniform, so the tighter of the two edges is the one that
    binds.
    """
    depth = breathe_level(str(item.fields.get("strength") or st.breathe), st.orientation)
    rect = item.fields.get("rect")
    if not isinstance(rect, dict):
        return depth, 0.5, 0.5
    try:
        width = max(0.05, min(1.0, float(rect.get("w", 1.0))))
        height = max(0.05, min(1.0, float(rect.get("h", 1.0))))
        x = float(rect.get("x", 0.0))
        y = float(rect.get("y", 0.0))
    except (TypeError, ValueError):
        return depth, 0.5, 0.5
    ceiling = (1.0 / min(width, height) - 1.0) / 2.0
    return min(depth, ceiling), x + width / 2, y + height / 2


def breathe_spans(st: ProjectState) -> list[tuple[float, float, float, float, float]]:
    """Stretches where the breathing amplitude is not the project default.

    Two things override it, and they use one mechanism because they are the
    same statement about the frame. A `breathe` block says "here, breathe this
    much, towards here". A camera block says "here, I am the move" - and that
    is an override to zero, because stacking a punch-in on top of a pulse is
    the one way this effect turns into seasickness.
    """
    spans: list[tuple[float, float, float, float, float]] = []
    for item in st.instances:
        if item.duration <= 0:
            continue
        if item.template == "breathe":
            spans.append((item.start, item.start + item.duration,
                          *breathe_framing(st, item)))
        elif TEMPLATE_PACKS.get(item.template, {}).get("camera"):
            spans.append((item.start, item.start + item.duration, 0.0, 0.5, 0.5))
    return sorted(spans)


def _breathe_expr(st: ProjectState, time_expr: str, index: int, base: float) -> str:
    """One breathing quantity at `time_expr`, as an FFmpeg expression.

    Piecewise linear: `base` everywhere, plus one trapezoid per override that
    ramps its difference in and back out again. Overlapping overrides would sum,
    so they are resolved into disjoint spans first. The amplitude and both
    centre axes share this, and share the ramps with it, so a block that is
    both deeper and off-centre arrives and leaves as one movement, not three.
    """
    parts = [f"{base:.9f}"]
    previous_end = -1e9
    for span in breathe_spans(st):
        start, end = max(span[0], previous_end), span[1]
        if end - start <= 1e-6:
            continue
        previous_end = end
        ramp = max(1e-3, min(BREATHE_RAMP, (end - start) / 3))
        window = (f"clip(min(({time_expr}-{start:.9f})/{ramp:.9f},"
                  f"({end:.9f}-{time_expr})/{ramp:.9f}),0,1)")
        parts.append(f"{span[index] - base:.9f}*{window}")
    return "(" + "+".join(parts) + ")"


def breathe_amplitude_expr(st: ProjectState, time_expr: str) -> str:
    """Breathing half-amplitude at `time_expr`, as an FFmpeg expression."""
    return _breathe_expr(st, time_expr, 2, breathe_level(st.breathe, st.orientation))


def breathe_centre_expr(st: ProjectState, time_expr: str, axis: str) -> str:
    """Where the breath pulls at `time_expr`, in 0..1 frame coordinates."""
    return _breathe_expr(st, time_expr, 3 if axis == "x" else 4, 0.5)


def breathes(st: ProjectState) -> bool:
    """Is anything on this timeline actually breathing?

    Asked before a filter is built, so a project with the effect off keeps the
    stream-copy path it has always had instead of paying for a zoompan that
    multiplies everything by one.
    """
    if breathe_level(st.breathe, st.orientation) > 0:
        return True
    return any(span[2] > 0 for span in breathe_spans(st))


def breathe_filter_graph(
    st: ProjectState, seg: dict, fps: float, source: str, out: str,
    size: Optional[tuple[int, int]] = None,
) -> str:
    """The breathing move for a segment that has no camera block of its own."""
    if not breathes(st):
        return f"{source}setpts=PTS-STARTPTS{out}"
    global_time = f"({seg['tl']:.9f}+on/{fps:.9f})"
    amplitude = breathe_amplitude_expr(st, global_time)
    # 1 + a + a*sin(...) rather than 1 + a*sin(...): the frame is only ever
    # scaled up, because scaling below 1 would pull the edges of the picture
    # into shot.
    zoom = (f"(1+{amplitude}+{amplitude}*"
            f"sin(2*PI*{BREATHE_HZ:.9f}*{global_time}))")
    width, height = size or (st.source_info.width, st.source_info.height)

    # `perspective`, not `zoompan`, and that swap is the whole of why the
    # breathing was reported as wobbling. zoompan crops an integer rectangle:
    # it truncates the crop size and the crop origin separately, so as the zoom
    # creeps the two round at different moments and the centre of the picture
    # slides between -1.0 and -0.5 pixels, flipping on nearly every frame. On a
    # move that travels a long way that is lost inside the movement; on a breath
    # that travels two per cent over four seconds it *is* the movement, and it
    # reads as a shiver. Measured on a static 1080p source, the mean
    # frame-to-frame difference under zoompan alternates across a thirty per
    # cent band where perspective walks smoothly through the same values -
    # perspective samples at 1/256 of a pixel, so there is nothing to snap to.
    #
    # It costs nothing extra: the warp is a pure centred zoom, its output is its
    # input size, and the trailing scale is a no-op whenever the frame already
    # is the size being asked for.
    half_w, half_h = f"(W/(2*{zoom}))", f"(H/(2*{zoom}))"
    centre_x = f"clip({breathe_centre_expr(st, global_time, 'x')}*W,{half_w},W-{half_w})"
    centre_y = f"clip({breathe_centre_expr(st, global_time, 'y')}*H,{half_h},H-{half_h})"
    left, right = f"({centre_x})-{half_w}", f"({centre_x})+{half_w}"
    top, bottom = f"({centre_y})-{half_h}", f"({centre_y})+{half_h}"
    return (
        f"{source}setpts=PTS-STARTPTS,fps={fps:.9f},"
        f"perspective=x0='{left}':y0='{top}':x1='{right}':y1='{top}':"
        f"x2='{left}':y2='{bottom}':x3='{right}':y3='{bottom}':"
        f"interpolation=cubic:sense=source:eval=frame,"
        f"scale={width}:{height}"
        f"{out}"
    )


def camera_plan(st: ProjectState, camera: Instance, fps: float) -> dict:
    """Resolve one camera instance into zoom/centre expressions of global time.

    The preview moves the footage with a CSS transform and the export moves it
    with an FFmpeg filter. Both read the same declarative `camera` block from
    template.json, so what was approved on the timeline is what gets rendered.
    """
    spec = TEMPLATE_PACKS.get(camera.template, {})
    settings = spec.get("camera", {}) or {}
    mode = settings.get("mode", "hold")
    animation = spec.get("animation", {})
    motion = spec.get("motion", {})
    entrance = float(motion.get("in", {}).get("ms", animation.get("entrance_ms", 450))) / 1000
    exit_ms = float(motion.get("out", {}).get("ms", animation.get("exit_ms", 400))) / 1000
    entrance = max(1 / fps, min(entrance, camera.duration * 0.7))
    exit_ms = max(1 / fps, min(exit_ms, camera.duration * 0.7))
    easing = settings.get("easing", "outCubic")

    def normalised(raw, fallback):
        rect = raw if isinstance(raw, dict) else fallback
        width = max(0.05, min(1.0, float(rect.get("w", 1.0))))
        height = max(0.05, min(1.0, float(rect.get("h", 1.0))))
        x = max(0.0, min(1.0 - width, float(rect.get("x", 0.0))))
        y = max(0.0, min(1.0 - height, float(rect.get("y", 0.0))))
        return {"x": x, "y": y, "w": width, "h": height,
                "cx": x + width / 2, "cy": y + height / 2,
                "zoom": 1.0 / max(width, height)}

    full = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    first = normalised(camera.fields.get("rect"), full)
    second = normalised(camera.fields.get("rect2"), camera.fields.get("rect") or full)

    start, end = camera.start, camera.start + camera.duration
    return {
        "mode": mode, "easing": easing, "entrance": entrance, "exit": exit_ms,
        "start": start, "end": end, "first": first, "second": second,
        "duration": max(1 / fps, camera.duration),
    }


def camera_expressions(plan: dict, time_expr: str) -> tuple[str, str, str]:
    """(zoom, centre-x, centre-y) as FFmpeg expressions over `time_expr`."""
    mode, easing = plan["mode"], plan["easing"]
    start, end = plan["start"], plan["end"]
    first, second = plan["first"], plan["second"]
    entrance, exit_ms, duration = plan["entrance"], plan["exit"], plan["duration"]
    local = f"clip({time_expr}-{start:.9f},0,{duration:.9f})"

    if mode == "reveal":
        k = _ease_expr(easing, f"clip({local}/{entrance:.9f},0,1)")
        amount = f"(1-{k})"
        zoom = f"(1+{first['zoom'] - 1:.9f}*{amount})"
        return zoom, f"{first['cx']:.9f}", f"{first['cy']:.9f}"

    if mode == "impact":
        # Cuts straight to the tight framing, then releases. No ease in: the
        # whole point is that the jump is felt on the frame it happens.
        release = max(1 / 30, duration * 0.62)
        k = _ease_expr(easing, f"clip({local}/{release:.9f},0,1)")
        zoom = f"(1+{first['zoom'] - 1:.9f}*(1-{k}))"
        return zoom, f"{first['cx']:.9f}", f"{first['cy']:.9f}"

    if mode in {"drift", "whip"}:
        curve = "inOutCubic" if mode == "drift" else "outQuint"
        k = _ease_expr(curve, f"clip({local}/{duration:.9f},0,1)")
        zoom = f"({first['zoom']:.9f}+{second['zoom'] - first['zoom']:.9f}*{k})"
        cx = f"({first['cx']:.9f}+{second['cx'] - first['cx']:.9f}*{k})"
        cy = f"({first['cy']:.9f}+{second['cy'] - first['cy']:.9f}*{k})"
        return zoom, cx, cy

    # "hold": ease in, stay, ease back out.
    in_k = _ease_expr(easing, f"clip({local}/{entrance:.9f},0,1)")
    out_k = _ease_expr(easing, f"clip(({end:.9f}-{time_expr})/{exit_ms:.9f},0,1)")
    amount = (
        f"if(lt({time_expr},{start + entrance:.9f}),{in_k},"
        f"if(gt({time_expr},{end - exit_ms:.9f}),{out_k},1))"
    )
    zoom = f"(1+{first['zoom'] - 1:.9f}*{amount})"
    return zoom, f"{first['cx']:.9f}", f"{first['cy']:.9f}"


def camera_filter_graph(
    st: ProjectState, seg: dict, fps: float, source: str = "[0:v]",
    out: str = "[base]", size: Optional[tuple[int, int]] = None,
) -> str:
    """Build the source-video transform for one segment.

    `source` is the label the transform reads from, so the look can be applied
    ahead of it and the camera can magnify an already-defocused frame. `out`
    and `size` exist so the identical move can be applied to the subject matte:
    a block composited behind the speaker is only behind them if the silhouette
    it is cut against was zoomed and panned exactly as the footage was.
    """
    camera_id = seg.get("camera")
    camera = next((item for item in st.instances if item.id == camera_id), None)
    if camera is None:
        midpoint = seg["tl"] + (seg["src_out"] - seg["src_in"]) / 2
        camera = camera_effect_at(st, midpoint)
    if camera is None:
        # No hand-placed move here, so the project's breathing is what moves
        # the frame. It returns the plain setpts when nothing is breathing, so
        # a project with the effect off builds the graph it always built.
        return breathe_filter_graph(st, seg, fps, source, out, size)

    plan = camera_plan(st, camera, fps)
    global_time = f"({seg['tl']:.9f}+on/{fps:.9f})"
    zoom, cx, cy = camera_expressions(plan, global_time)
    width, height = size or (st.source_info.width, st.source_info.height)
    peak = max(plan["first"]["zoom"], plan["second"]["zoom"])
    # zoompan samples the frame it is handed. Handing it a 2x lanczos upscale
    # first is what separates a punch-in that looks intentional from one that
    # looks like a soft crop, and it also halves zoompan's integer-pixel jitter.
    supersample = ",scale=iw*2:ih*2:flags=lanczos" if peak > 1.12 else ""
    return (
        f"{source}setpts=PTS-STARTPTS{supersample},"
        f"zoompan=z='{zoom}':"
        f"x='clip({cx}*iw-iw/(2*zoom),0,iw-iw/zoom)':"
        f"y='clip({cy}*ih-ih/(2*zoom),0,ih-ih/zoom)':"
        f"d=1:s={width}x{height}:fps={fps:.9f}"
        f"{out}"
    )


def punch_filter_graph(st: ProjectState, seg: dict, fps: float) -> str:
    """Backward-compatible name for older tests and integrations."""
    return camera_filter_graph(st, seg, fps)


# ------------------------------------------------- backdrop blur and depth
# One number, deliberately. The blur radius is a fraction of the frame's short
# edge rather than a pixel count, so the same value reads identically on a 720p
# landscape proxy and a 4K vertical master - the same reason the look's defocus
# is expressed this way. At 0.040 the background keeps its shapes and colours
# and loses all its detail, which is what separates "an overlay in front of a
# room" from "an overlay pasted on a photo".
BACKDROP_BLUR = 0.040
# Blurring *raises* a frame's average brightness, because it spreads highlights
# across their neighbours. Left alone, white text over a blurred bright wall
# loses the contrast it had over the sharp one. This pulls the blurred plate
# back down by a tenth, which cancels that. It is a legibility correction, not
# a mood, which is why it is a constant and not a control.
BACKDROP_DIM = 0.10

# Fixed order. The renderer packs the slots it was asked for into one strip in
# this order and the filter graph crops them back out in this order, so the two
# never need to negotiate anything beyond the list itself.
SLOT_ORDER = ("below", "mask", "behind", "front")


def segment_instances(st: ProjectState, seg: dict) -> list[Instance]:
    """Every block that is on screen at any point during one segment."""
    start = seg["tl"]
    end = start + (seg["src_out"] - seg["src_in"])
    return [item for item in st.instances
            if item.start < end and start < item.start + item.duration]


def matte_ready(st: ProjectState) -> bool:
    """Is there a subject matte matching the current footage?

    Distinct from `look_state()["defocus"]`, which also asks whether the user
    turned the defocus slider up. A block placed behind the speaker needs the
    silhouette regardless of whether the look is using it.
    """
    return (st.look.matte_revision == st.source_revision
            and matte_path(st.name).is_file())


def effective_depth(item: Instance) -> str:
    """Which side of the speaker this block composites on.

    Nearly always the block's own toggle. A pack may instead declare
    `depth_from` - a field name plus the values of it that mean "behind" - for
    templates where the side is not a separate decision but the meaning of a
    field the editor already set. The stage backdrop is the case that needs it:
    choosing "behind" as its framing IS the depth, and asking the same question
    again in the compositing row would be two controls for one intent, which is
    exactly how a timeline ends up with a block that says one thing and renders
    another.
    """
    rule = TEMPLATE_PACKS.get(item.template, {}).get("depth_from")
    if isinstance(rule, dict) and rule.get("field"):
        value = item.fields.get(rule["field"], rule.get("default"))
        return "behind" if value in (rule.get("behind") or []) else "front"
    return item.depth


def overlay_slots(st: ProjectState, seg: dict) -> list[str]:
    """Which overlay planes this segment needs, in SLOT_ORDER.

    A segment with no backdrop and nothing behind the speaker returns just
    ["front"], which is the graph Editoro has always built - one overlay, one
    composite. The extra planes cost extra pixels through the PNG pipe, so they
    are only asked for where a block actually uses them.
    """
    active = segment_instances(st, seg)
    slots: list[str] = []
    if any(item.backdrop for item in active):
        slots += ["below", "mask"]
    if matte_ready(st) and any(effective_depth(item) == "behind" for item in active):
        slots.append("behind")
    slots.append("front")
    return slots


def overlay_composite_graph(
    slots: list[str], base: str, overlay: str, out: str,
    width: int, height: int, matte: Optional[str],
) -> str:
    """Composite the packed overlay strip around the footage.

    The order is the whole point:

        footage -> below -> BLUR -> behind -> speaker -> front

    "below" and "front" are the two halves of the stack split at the lowest
    block that asked for a backdrop; the blur therefore softens the footage and
    every block underneath that one, and leaves that block and everything above
    it sharp. "behind" lands after the blur but before the speaker is composited
    back over the top, which is what puts a graphic behind their head.

    The blur is applied everywhere and then masked back in by the alpha of the
    "mask" plane, rather than being switched on and off by a timeline enable.
    That is what lets it fade: the strength is drawn by the template's own
    motion curve, in the same place every other transition in Editoro lives.
    """
    parts: list[str] = []
    count = len(slots)
    if count == 1:
        parts.append(f"{overlay}setpts=PTS-STARTPTS[ovfront]")
    else:
        parts.append(f"{overlay}setpts=PTS-STARTPTS,split={count}"
                     + "".join(f"[strip{i}]" for i in range(count)))
        for index, slot in enumerate(slots):
            parts.append(
                f"[strip{index}]crop={width}:{height}:{index * width}:0[ov{slot}]"
            )
    label = base
    if "below" in slots:
        parts.append(f"{label}[ovbelow]overlay=0:0:format=auto:eof_action=pass:shortest=0[bdbase]")
        sigma = BACKDROP_BLUR * min(width, height)
        parts.append("[bdbase]split=2[bdsharp][bdsoft]")
        parts.append(
            f"[bdsoft]gblur=sigma={sigma:.3f}:steps=3,"
            f"eq=brightness=-{BACKDROP_DIM:.3f}[bddim]"
        )
        parts.append("[ovmask]alphaextract[bdmask]")
        parts.append("[bddim][bdmask]alphamerge[bdcut]")
        parts.append("[bdsharp][bdcut]overlay=0:0:format=auto[bdout]")
        label = "[bdout]"
    if "behind" in slots and matte:
        # The speaker is lifted off the plate as it stands *now* - after the
        # blur, not before it. If a backdrop is softening the frame, it softens
        # the speaker too; they are part of what is underneath it.
        parts.append(f"{label}split=2[dpplate][dpsubject]")
        parts.append(f"{matte}format=gray[dpmask]")
        parts.append("[dpsubject][dpmask]alphamerge[dpcut]")
        parts.append("[dpplate][ovbehind]overlay=0:0:format=auto:eof_action=pass:shortest=0[dpback]")
        parts.append("[dpback][dpcut]overlay=0:0:format=auto[dpout]")
        label = "[dpout]"
    parts.append(f"{label}[ovfront]overlay=0:0:format=auto:eof_action=pass:shortest=0{out}")
    return ";".join(parts)


def export_dimensions(st: ProjectState, resolution: str) -> tuple[int, int]:
    """The pixel size one export writes, for a `resolution` the API accepts.

    Both the overlay renderer and the FFmpeg graph need this, and they have to
    agree exactly: the overlay is composited at the output size now rather than
    scaled down afterwards, so a disagreement of one pixel is a visible offset
    rather than a rounding detail.
    """
    width, height = st.source_info.width, st.source_info.height
    if resolution == "source" or not height:
        return width, height
    target_h = int(resolution)
    target_w = int(round(target_h * width / height / 2) * 2)
    return target_w, target_h


# --------------------------------------------------------- scrub proxies
# The single most expensive thing in an export used to be invisible. Templates
# that draw footage - the PiP window, every b-roll clip - read it out of a
# <video> element, and the headless renderer has to put that element on an
# exact timestamp once per exported frame. Seeking a long-GOP H.264 master
# means decoding every frame from the preceding keyframe forward, so on a 4K
# master that one line of JavaScript cost 5.8 seconds per frame, and a
# two-minute edit spent the better part of an hour inside it.
#
# An all-intra copy makes every frame its own keyframe, so the same seek
# decodes exactly one frame. It is a throwaway file built with NVENC where
# there is NVENC, at the size the export actually writes and never larger than
# the source, and it is cached by content so a second export of the same
# project pays nothing.
SCRUB_GOP = 1                 # all-intra: the whole point
EXPORT_RENDER_LANES = 2       # browser pages rasterising at once
EXPORT_BATCH_TIMEOUT = 20.0   # seconds per frame before a lane is declared stalled
SCRUB_CACHE_BUDGET = 8 << 30  # bytes of scrub proxies kept per project


def _transparent_png(width: int, height: int) -> bytes:
    """A fully transparent RGBA PNG, written by hand.

    Every row of a blank overlay is identical and every byte of it is zero, so
    the deflate stream compresses to almost nothing and this is far cheaper
    than asking the browser for a picture of nothing. Written directly rather
    than through a library because the only image Editoro ever needs to
    *create* from scratch is this one.
    """
    import zlib

    # One filter byte (0 = None) per row, then width * 4 zero bytes of RGBA.
    raw = (b"\x00" + b"\x00" * (width * 4)) * height

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    return b"".join([
        b"\x89PNG\r\n\x1a\n",
        chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
        chunk(b"IDAT", zlib.compress(raw, 6)),
        chunk(b"IEND", b""),
    ])


def scrub_dir(project: str) -> Path:
    return project_dir(project) / "scrub"


def scrub_encoder() -> list[str]:
    """Fast, all-intra, visually lossless enough to draw from."""
    # All-intra costs a lot of bits - a 4K master runs to several gigabytes -
    # and every one of these frames is drawn at the export resolution or
    # smaller and then re-encoded by the real encoder afterwards. So this sits
    # a little below the quality the final file is written at: comfortably
    # invisible once composited, and roughly half the disk of qp 20.
    if nvenc_available():
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull",
                "-rc", "constqp", "-qp", "23"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "21",
            "-tune", "fastdecode"]


def scrub_size(width: int, height: int, target_w: int, target_h: int) -> tuple[int, int]:
    """The render size, but never an upscale - that would only cost time."""
    if not width or not height:
        return target_w, target_h
    scale = min(1.0, max(target_w / width, target_h / height))
    return (max(2, int(round(width * scale / 2)) * 2),
            max(2, int(round(height * scale / 2)) * 2))


def build_scrub_proxy(project: str, media: Path, target_w: int, target_h: int,
                      log_file: Optional[Path] = None,
                      build: bool = True) -> Optional[Path]:
    """One all-intra copy of `media`, cached. Returns None if it cannot be made.

    Failure is deliberately soft: a missing scrub copy means the renderer falls
    back to seeking the original, which is exactly what it did before. Slow is
    a far better failure than broken.
    """
    try:
        info = probe(media)
    except Exception:
        return None
    width, height = scrub_size(info.width, info.height, target_w, target_h)
    key = _short_key(
        f"{media.name}:{media.stat().st_mtime_ns}:{media.stat().st_size}"
        f":{width}x{height}:{SCRUB_GOP}"
    )
    target = scrub_dir(project) / f"{key}.mp4"
    if target.is_file():
        return target
    if not build:
        # Still frames reuse a proxy an export already left behind, but never
        # transcode a whole master to answer one frame - that would turn a
        # preview click into a minutes-long wait.
        return None
    scrub_dir(project).mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{key}.part.mp4")
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(media),
        "-vf", f"scale={width}:{height}:flags=bicubic",
        *scrub_encoder(),
        "-g", str(SCRUB_GOP), "-bf", "0",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(partial),
    ]
    result = run(command, log_file)
    if result.returncode or not partial.is_file():
        partial.unlink(missing_ok=True)
        _append_log(log_file, "  scrub proxy failed; the renderer will seek "
                              "the original instead (slower, still correct)")
        return None
    os.replace(partial, target)
    return target


def export_scrub_map(st: ProjectState, width: int, height: int,
                     log_file: Optional[Path] = None,
                     build: bool = True) -> dict[str, str]:
    """{media file name: scrub URL} for every video the renderer will seek.

    Keyed by bare file name rather than by URL on purpose. The page decorates
    its media URLs with cache-busting signatures that this code would have to
    reproduce byte for byte to match on, and a near-miss would fail silently by
    simply never substituting - the export would still be correct and still be
    slow, which is the hardest kind of bug to notice. A file name is something
    both sides can agree on without either one knowing how the other builds a
    URL.
    """
    project = st.name
    wanted: list[tuple[str, Path]] = []
    if st.source and any(
        (TEMPLATE_PACKS.get(item.template) or {}).get("source_window")
        for item in st.instances
    ):
        wanted.append((st.source, project_dir(project) / st.source))
    video_suffixes = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
    for item in st.instances:
        for key in ("asset", "clip"):
            asset = item.fields.get(key)
            if not isinstance(asset, str) or not asset:
                continue
            path = project_dir(project) / "assets" / asset
            if path.suffix.lower() in video_suffixes and path.is_file():
                wanted.append((asset, path))
    mapping: dict[str, str] = {}
    for name, path in wanted:
        if name in mapping:
            continue
        proxy = build_scrub_proxy(project, path, width, height, log_file, build)
        if proxy:
            mapping[name] = f"/media/{project}/scrub/{proxy.name}"
    if mapping:
        _append_log(log_file, f"  scrub proxies ready for {len(mapping)} video "
                              f"source(s) at {width}x{height}")
        prune_scrub_cache(project, keep={Path(url).name for url in mapping.values()})
    return mapping


def prune_scrub_cache(project: str, keep: set[str],
                      budget: int = SCRUB_CACHE_BUDGET) -> None:
    """Keep the scrub folder from growing without limit.

    These are worth caching - rebuilding the set for a busy project took nearly
    three minutes - but an all-intra copy of a 4K master is measured in
    gigabytes, and a project exported at three resolutions would keep three of
    everything forever. So: never touch what this export is about to use, then
    drop the least recently used of the rest until the folder is under budget.
    """
    directory = scrub_dir(project)
    if not directory.is_dir():
        return
    try:
        others = sorted(
            (item for item in directory.glob("*.mp4") if item.name not in keep),
            key=lambda item: item.stat().st_mtime,
        )
        total = sum(item.stat().st_size for item in directory.glob("*.mp4"))
    except OSError:
        return
    for item in others:
        if total <= budget:
            break
        try:
            size = item.stat().st_size
            item.unlink()
            total -= size
        except OSError:
            continue


class OverlayRenderSession:
    """One shared browser page per export, regardless of dirty segment count."""

    def __init__(self, name: str, state: ProjectState,
                 width: int = 0, height: int = 0,
                 scrub: Optional[dict[str, str]] = None):
        self.name = name
        self.state = state
        # {file name: scrub URL}. See export_scrub_map.
        self.scrub = scrub or {}
        # One fully transparent frame, encoded once and handed back for every
        # blank moment in the timeline. Built lazily because its size depends
        # on how many planes the segment asked for.
        self._blank: dict[int, bytes] = {}
        # Where this page's frames come back to. Frames are posted as binary
        # bodies rather than returned as base64 through CDP; the token keeps
        # concurrent exports from writing into each other's pile.
        self.token = uuid.uuid4().hex
        self._seq = 0
        # Set if the browser goes away underneath us. A renderer that dies must
        # surface as a failed export, never as an export that sits there: the
        # FFmpeg on the other end of the pipe is blocked reading stdin and will
        # wait for input that is never coming, forever.
        self.lost: asyncio.Event = asyncio.Event()
        # Whether frames come back as binary POSTs. Decided by a probe on the
        # first page; false means the data-URL path, which is slower and just
        # as correct.
        self.binary = True
        # The size the overlay is rasterised at. Defaults to the source, but an
        # export writing 720p asks for 720p frames: same picture, a fraction of
        # the PNG encode, transfer and decode. Templates measure themselves in
        # fractions of the frame, so nothing about the layout changes.
        self.width = width or state.source_info.width
        self.height = height or state.source_info.height
        self.playwright = None
        self.browser = None
        self.page = None
        self.pages: list[Any] = []
        # Rasterising is now the slowest thing in an export, and it is one
        # canvas on one thread. Chromium will happily run several pages at
        # once, and the machine has cores doing nothing, so the batches are
        # dealt round-robin across a small pool. Capped rather than scaled to
        # the core count: each page holds decoders and a full-size canvas, and
        # past about four they start competing for memory bandwidth instead of
        # adding throughput.
        # ...and fewer of them the larger the frame, because each lane holds a
        # full set of video decoders and two canvases of this size. Four lanes
        # of 4K is several gigabytes before any of them has drawn anything,
        # and a renderer that runs out of memory halfway through is a far worse
        # outcome than one that finishes a little slower.
        pixels = max(1, self.width * self.height)
        budget = 2 if pixels <= 1920 * 1080 else 1
        self.lanes = max(1, min(EXPORT_RENDER_LANES, budget,
                                (os.cpu_count() or 4) // 2))
        self.errors: list[str] = []

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        executable = renderer_executable()
        if not executable:
            raise RuntimeError(
                "The export renderer is not installed. Close Editoro, run launch.cmd once, "
                "and let setup finish."
            )
        EXPORT_FRAMES[self.token] = {}
        self.playwright = await async_playwright().start()
        self.lost = asyncio.Event()
        self.browser = await self.playwright.chromium.launch(
            executable_path=str(executable),
            args=[
                "--disable-gpu-sandbox",
                "--enable-gpu-rasterization",
                "--enable-zero-copy",
                "--ignore-gpu-blocklist",
                "--use-angle=d3d11",
            ],
        )
        self.browser.on("disconnected", lambda _: self.lost.set())
        snapshot = json.dumps(self.state.model_dump(), ensure_ascii=True)
        scrub = json.dumps(self.scrub, ensure_ascii=True)
        for _ in range(self.lanes):
            self.pages.append(await self._open_page(snapshot, scrub))
        self.page = self.pages[0]   # the one a single still frame uses

    async def _open_page(self, snapshot: str, scrub: str):
        page = await self.browser.new_page(viewport={
            "width": self.width,
            "height": self.height,
        })
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on(
            "console",
            lambda message: self.errors.append(message.text)
            if message.type == "error" else None,
        )
        await page.add_init_script(
            f"window.__EDITORO_EXPORT_STATE = {snapshot};"
            f"window.__EDITORO_SCRUB = {scrub};"
        )
        await page.goto(
            f"http://127.0.0.1:{PORT}/?export=1&project={self.name}"
            f"&w={self.width}&h={self.height}"
        )
        try:
            await page.wait_for_function(
                "window.__exportReady === true || window.__exportError",
                timeout=60000,
            )
        except Exception as exc:
            detail = " | ".join(self.errors[-5:]) or str(exc)
            raise RuntimeError(f"Export renderer did not become ready: {detail}") from exc
        setup_error = await page.evaluate("window.__exportError || null")
        if setup_error:
            raise RuntimeError(f"Export renderer failed to start: {setup_error}")
        # Ask once whether frames can come back the fast way. See __exportProbe.
        if self.binary:
            try:
                self.binary = bool(await page.evaluate(
                    "token => window.__exportProbe(token)", self.token))
            except Exception:
                self.binary = False
            if not self.binary:
                # The probe's own failure is logged to the page console by the
                # browser; it is expected, handled, and must not be mistaken
                # for a template blowing up.
                self.errors.clear()
        EXPORT_FRAMES.get(self.token, {}).pop(-1, None)
        return page

    async def _render_batch(self, times: list[float], slots: list[str],
                            page=None) -> list[bytes]:
        first = self._seq
        self._seq += len(times)
        results = await (page or self.page).evaluate(
            "args => window.__renderFrames(args.times, args.slots, "
            "args.token, args.first)",
            {"times": times, "slots": slots,
             "token": self.token if self.binary else None, "first": first},
        )
        sink = EXPORT_FRAMES.get(self.token) or {}
        frames: list[bytes] = []
        for item in results:
            # Three shapes, in the order they cost: "" is a moment with nothing
            # on it, which the page skipped entirely; an int is a frame already
            # posted back as binary; a data URL is the old path, still handled
            # so a still frame and a stale page both keep working.
            if item == "" or item is None:
                frames.append(self.blank_frame(len(slots)))
            elif isinstance(item, (int, float)):
                data = sink.pop(int(item), None)
                if data is None:
                    raise RuntimeError(
                        f"overlay frame {int(item)} never arrived from the renderer")
                frames.append(data)
            else:
                frames.append(base64.b64decode(str(item).split(",", 1)[1]))
        return frames

    async def _await_batch(self, task: asyncio.Task, size: int) -> list[bytes]:
        """Wait for one batch, but never forever.

        Two things can leave a rendered batch pending for the rest of time, and
        both of them used to strand the export rather than end it: the browser
        dying (its pending evaluate is simply never settled), and a page that is
        alive but wedged. Downstream, FFmpeg is blocked reading a pipe, so the
        whole export sits at a fixed percentage looking like it is working. An
        export that fails with a sentence is strictly better than one that
        hangs, so this turns both cases into an exception.

        The budget is per frame and deliberately generous - a 4K frame carrying
        several video seeks measured well under a second, so seconds a frame is
        far outside anything healthy and only trips on a genuine stall.
        """
        watchers = [asyncio.ensure_future(self.lost.wait())]
        try:
            done, _ = await asyncio.wait(
                [task, *watchers],
                timeout=EXPORT_BATCH_TIMEOUT * max(1, size),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return task.result()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.lost.is_set():
                raise RuntimeError(
                    "The export renderer stopped responding - the browser it "
                    "runs in went away mid-export. This is usually memory: try "
                    "exporting at a smaller resolution."
                )
            raise RuntimeError(
                f"The export renderer stalled on a batch of {size} frame(s) and "
                f"was given up on after "
                f"{EXPORT_BATCH_TIMEOUT * max(1, size):.0f}s."
            )
        finally:
            for watcher in watchers:
                watcher.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)

    def blank_frame(self, planes: int) -> bytes:
        """A transparent PNG the size of one strip, encoded once per width."""
        if planes not in self._blank:
            self._blank[planes] = _transparent_png(self.width * planes, self.height)
        return self._blank[planes]

    async def frame_batches(
        self,
        seg: dict,
        cancel: asyncio.Event,
        batch_size: int = 8,
        slots: Optional[list[str]] = None,
    ):
        """Yield rendered overlay batches in order, keeping every lane busy.

        The caller writes each batch into FFmpeg's stdin and waits for the pipe
        to drain, so anything rendered during that wait is free. Rasterising is
        the slowest part of an export and it is single-threaded per page, so
        several batches are kept in flight across the page pool at once and
        handed back strictly in order - FFmpeg is being fed a video stream and
        cannot take frame 40 before frame 32.

        Exactly `lanes` batches are ever outstanding, so memory stays bounded
        at lanes * batch_size frames however long the segment is.
        """
        if not self.pages:
            raise RuntimeError("overlay renderer is not started")
        slots = slots or ["front"]
        fps = self.state.source_info.fps or 30
        count = max(1, int(math.ceil((seg["src_out"] - seg["src_in"]) * fps)))
        starts = list(range(0, count, batch_size))

        def times_for(first: int) -> list[float]:
            return [
                seg["tl"] + frame / fps
                for frame in range(first, min(count, first + batch_size))
            ]

        inflight: "collections.deque[tuple[int, asyncio.Task]]" = collections.deque()
        next_batch = 0

        def fill() -> None:
            """Start batches until every lane has one."""
            nonlocal next_batch
            while len(inflight) < len(self.pages) and next_batch < len(starts):
                first = starts[next_batch]
                page = self.pages[next_batch % len(self.pages)]
                inflight.append((
                    first,
                    asyncio.create_task(self._render_batch(times_for(first), slots, page)),
                ))
                next_batch += 1

        try:
            fill()
            while inflight:
                if cancel.is_set():
                    raise asyncio.CancelledError()
                first, task = inflight.popleft()
                frames = await self._await_batch(task, len(times_for(first)))
                # Refill only after one has been taken, so a lane is never
                # asked for two batches at once.
                fill()
                yield first, count, frames
        finally:
            for _, task in inflight:
                task.cancel()
            await asyncio.gather(*(task for _, task in inflight),
                                 return_exceptions=True)
        if self.errors:
            raise RuntimeError(
                "Template renderer error: " + " | ".join(self.errors[-5:])
            )

    async def close(self) -> None:
        # Drop the pile first: a frame still in flight when the page goes away
        # should be refused rather than kept for an export that has finished.
        EXPORT_FRAMES.pop(self.token, None)
        self.pages = []
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.browser = self.playwright = self.page = None


async def encode_dirty_segment(
    renderer: OverlayRenderSession,
    st: ProjectState,
    seg: dict,
    src: Path,
    part: Path,
    vcodec: list[str],
    resolution: str,
    log: Path,
    cancel: asyncio.Event,
    project: str,
    segment_index: int,
    segment_total: int,
) -> None:
    """Stream transparent PNG frames straight into FFmpeg without disk files."""
    fps = st.source_info.fps or 30
    duration = seg["src_out"] - seg["src_in"]
    frames = segment_frames(seg, fps)
    # Which overlay planes this span needs. Most spans need only "front", and
    # then everything below builds the same single-composite graph as before.
    slots = overlay_slots(st, seg)
    # The matte, when there is one, is the third input: source, overlay pipe,
    # depth. It is seeked to the same source moment, so the two videos are
    # frame-aligned without any further bookkeeping.
    matte = look_inputs(st, seg["src_in"], duration, need_matte="behind" in slots)
    source_filter = base_video_graph(st, seg, fps, 2 if matte else None)
    # Scale the footage BEFORE the overlay goes on, not after. The overlay is
    # rendered at the output size, so compositing at the output size is what
    # keeps the two aligned - and it means the expensive lanczos pass runs on
    # the source frame alone instead of on the composite.
    width, height = export_dimensions(st, resolution)
    scale_filter = ""
    if resolution != "source":
        scale_filter = f"scale={width}:{height}:flags=lanczos,"
    # A block behind the speaker is cut against the matte, so the matte has to
    # have been through the same camera move as the footage - otherwise a
    # punch-in slides the silhouette off the person it was cut from.
    matte_label = None
    matte_prep = ""
    if "behind" in slots and matte:
        matte_prep = (
            f"[2:v]setpts=PTS-STARTPTS,scale={width}:{height}"
            f":flags=bicubic:in_range=tv:out_range=pc[dpraw];"
            + camera_filter_graph(st, seg, fps, "[dpraw]", "[dpcam]", (width, height))
            + ";"
        )
        matte_label = "[dpcam]"
    composite = overlay_composite_graph(
        slots, "[padded]", "[1:v]", "[composited]", width, height, matte_label,
    )
    # The base is padded with a cloned final frame and the output is bounded by
    # an exact frame count. Previously this relied on overlay's `shortest`, so a
    # single rounding disagreement between the decoder and the frame generator
    # shortened the span and every later cut drifted against the audio.
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *look_hw_args(st),
        "-ss", f"{seg['src_in']:.6f}", "-t", f"{duration + 0.5:.6f}", "-i", str(src),
        "-thread_queue_size", "64", "-f", "image2pipe",
        "-framerate", f"{fps:.6f}", "-vcodec", "png", "-i", "pipe:0",
        *matte,
        "-filter_complex",
        f"{source_filter};[base]{scale_filter}tpad=stop=-1:stop_mode=clone[padded];"
        f"{matte_prep}{composite};"
        f"[composited]fps={fps:.6f}[v]",
        "-map", "[v]", *vcodec, "-frames:v", str(frames), "-an",
        *bitstream_filter(st), "-f", "mpegts", str(part),
    ]
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("\n$ " + " ".join(command) + "\n")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        creationflags=CHILD_FLAGS,
    )
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        async for first, count, frames in renderer.frame_batches(seg, cancel, slots=slots):
            if cancel.is_set():
                raise asyncio.CancelledError()
            if process.returncode is not None:
                break
            for frame in frames:
                process.stdin.write(frame)
            await process.stdin.drain()
            await progress(
                project,
                seg=segment_index,
                total=segment_total,
                status="render",
                frame=min(count, first + len(frames)),
                frames=count,
            )
        if process.stdin:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        wait_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(cancel.wait())
        done, _ = await asyncio.wait(
            {wait_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and cancel.is_set() and not wait_task.done():
            process.terminate()
            try:
                await asyncio.wait_for(wait_task, timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await wait_task
            raise asyncio.CancelledError()
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        await wait_task
    except BaseException:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    finally:
        stderr = (await stderr_task).decode("utf-8", "replace")
        if stderr:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(stderr + "\n")
    if process.returncode:
        detail = stderr.strip()[-2000:] or "FFmpeg stopped while receiving overlay frames"
        raise RuntimeError(f"FFmpeg failed ({process.returncode}): {detail}")


FRAME_LOCK = asyncio.Lock()


async def render_still(name: str, timeline_time: float, width: int = 0) -> bytes:
    """One composited PNG of the edit at a timeline moment: footage + overlays.

    This is what lets an agent actually look at what it built instead of
    reasoning about coordinates blind. It runs the same renderer the export
    uses, so the still is not an approximation of the result - it is a frame of
    it, camera move included.
    """
    st = load_state(name)
    folder = project_dir(name)
    if not st.source or not (folder / st.source).is_file():
        raise HTTPException(400, "Import source footage first")
    duration = timeline_duration(st)
    fps = st.source_info.fps or 30
    timeline_time = max(0.0, min(timeline_time, max(0.0, duration - 1 / fps)))
    source_time = timeline_to_source(st, timeline_time)
    async with FRAME_LOCK:
        with tempfile.TemporaryDirectory(prefix="editoro-frame-") as scratch:
            work = Path(scratch)
            base = work / "base.png"
            camera = camera_effect_at(st, timeline_time)
            segment = {"src_in": source_time, "src_out": source_time + 1 / fps,
                       "tl": timeline_time, "camera": camera.id if camera else None}
            # The still has to go through the same planes the export does, or
            # an agent checking its own work sees a frost that is not there and
            # a block in front of the speaker it asked to put behind them.
            slots = overlay_slots(st, segment)
            matte = look_inputs(st, source_time, 1 / fps, need_matte="behind" in slots)
            graph = base_video_graph(st, segment, fps, 1 if matte else None)
            command = [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *look_hw_args(st),
                "-ss", f"{source_time:.6f}", "-i", str(folder / st.source), *matte,
                "-filter_complex", graph, "-map", "[base]", "-frames:v", "1", str(base),
            ]
            result = run(command)
            if result.returncode or not base.is_file():
                raise HTTPException(500, (result.stderr or "could not read that frame")[-400:])

            renderer = OverlayRenderSession(
                name, st,
                scrub=export_scrub_map(st, st.source_info.width,
                                       st.source_info.height, build=False),
            )
            await renderer.start()
            try:
                data_url = await renderer.page.evaluate(
                    "args => window.__renderFrame(args.t, args.slots)",
                    {"t": timeline_time, "slots": slots})
            finally:
                await renderer.close()
            overlay = work / "overlay.png"
            overlay.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))

            out = work / "frame.png"
            scale = f",scale={int(width)}:-2:flags=lanczos" if width else ""
            frame_w, frame_h = st.source_info.width, st.source_info.height
            matte_prep, matte_label = "", None
            if "behind" in slots and matte:
                # Input 2 here, not 3: this command re-reads the base PNG rather
                # than the source, so the matte is the second extra input.
                matte_prep = (
                    f"[2:v]scale={frame_w}:{frame_h}"
                    f":flags=bicubic:in_range=tv:out_range=pc[dpraw];"
                    + camera_filter_graph(st, segment, fps, "[dpraw]", "[dpcam]",
                                          (frame_w, frame_h))
                    + ";"
                )
                matte_label = "[dpcam]"
            composite = overlay_composite_graph(
                slots, "[0:v]", "[1:v]", "[composited]",
                frame_w, frame_h, matte_label,
            )
            result = run([
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(base), "-i", str(overlay),
                # Seeked to the same source moment as the base frame, or the
                # silhouette would be the one from the start of the video.
                *(["-ss", f"{source_time:.6f}", "-i", str(matte_path(st.name))]
                  if matte_label else []),
                "-filter_complex",
                f"{matte_prep}{composite};[composited]null{scale}[v]",
                "-map", "[v]", "-frames:v", "1", str(out),
            ])
            if result.returncode or not out.is_file():
                raise HTTPException(500, (result.stderr or "could not compose that frame")[-400:])
            return out.read_bytes()


# ------------------------------------------------------------- the thumbnail
# The design is composed in the browser, by the same canvas code that draws the
# preview, and arrives here already rendered. That is deliberate: a thumbnail is
# a still, so there is nothing for the headless renderer to time, and asking the
# export pipeline to reproduce a second layout engine for one PNG would be two
# implementations of one picture - the exact trap the overlay engine exists to
# avoid. The browser draws it, the server stores it and knows how to hold it on
# the front of a video.
THUMBNAIL_SIZES = {"horizontal": (1280, 720), "vertical": (1080, 1920)}


def thumbnail_file(name: str, orientation: str) -> Path:
    return require_project(name) / f"thumbnail-{orientation}.png"


@app.post("/api/projects/{name}/thumbnail")
async def api_thumbnail_save(name: str, request: Request, orientation: str = "horizontal",
                             keep: bool = False):
    """Store the rendered thumbnail; `keep` also drops a dated copy in exports."""
    folder = require_project(name)
    if orientation not in THUMBNAIL_SIZES:
        raise HTTPException(400, "orientation must be horizontal or vertical")
    png = await request.body()
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(400, "expected a PNG body")
    if len(png) > 24 * 1024 * 1024:
        raise HTTPException(413, "thumbnail too large")
    thumbnail_file(name, orientation).write_bytes(png)
    saved = None
    if keep:
        (folder / "exports").mkdir(exist_ok=True)
        saved = folder / "exports" / f"{name}-thumb-{time.strftime('%Y%m%d-%H%M%S')}.png"
        saved.write_bytes(png)
    return {"ok": True, "bytes": len(png),
            "file": f"exports/{saved.name}" if saved else None}


@app.get("/api/projects/{name}/thumbnail")
def api_thumbnail_read(name: str, orientation: str = "horizontal"):
    path = thumbnail_file(name, orientation)
    if not path.is_file():
        raise HTTPException(404, "no thumbnail saved yet")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


async def prepend_thumbnail(
    st: ProjectState, name: str, finished: Path, work: Path, fps: float,
    vcodec: list[str], log: Path, cancel: asyncio.Event,
) -> None:
    """Hold the saved thumbnail on the front of a finished export, in place.

    Runs after the frame audit rather than inside the join, and that is on
    purpose. The audit exists to prove that every source frame survived the cut
    list; a deliberately added still would make it fail for a good reason, and a
    check that has to be relaxed to pass is not a check. So the video is built
    and verified exactly as it always was, and the cover is concatenated onto
    the front of the verified file afterwards.

    MPEG-TS for the join, for the same reason the spans use it: the still and
    the video carry different parameter sets and TS carries those inline.
    """
    still = thumbnail_file(name, st.orientation)
    if st.thumbnail.hold <= 0 or not still.is_file():
        return
    info = probe(finished)
    codec = (info.codec or "").lower()
    bitstream = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb",
                 "h265": "hevc_mp4toannexb"}.get(codec)
    if not bitstream:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"\nthumbnail hold skipped: cannot stream-join {codec or 'unknown'}\n")
        return
    width, height = info.width or st.source_info.width, info.height or st.source_info.height
    # Whole frames. A hold of "0.7 of a frame" is a hold of one frame with a
    # duration nobody can predict, and the join then lands off the grid.
    frames = max(1, round(st.thumbnail.hold * fps))
    cover, tail, joined = work / "cover.ts", work / "tail.ts", work / "withcover.mp4"
    await run_cancelable([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-framerate", f"{fps:.9f}", "-t", f"{frames / fps:.6f}", "-i", str(still),
        "-f", "lavfi", "-t", f"{frames / fps:.6f}", "-i", "anullsrc=r=48000:cl=stereo",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:-1:-1:color=black,format=yuv420p,fps={fps:.9f}",
        *vcodec, "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
        "-f", "mpegts", str(cover),
    ], log, cancel, check=True)
    await run_cancelable([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(finished),
        "-c", "copy", "-bsf:v", bitstream, "-f", "mpegts", str(tail),
    ], log, cancel, check=True)
    listing = work / "cover-list.txt"
    listing.write_text("".join(f"file '{path.as_posix()}'\n" for path in (cover, tail)),
                       encoding="utf-8")
    await run_cancelable([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", "-movflags", "+faststart", str(joined),
    ], log, cancel, check=True)
    os.replace(joined, finished)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(f"\nthumbnail held on the front for {frames} frame(s)\n")


@app.get("/api/projects/{name}/frame")
async def api_frame(name: str, t: float = 0, width: int = 1280):
    require_project(name)
    png = await render_still(name, t, max(160, min(3840, int(width))))
    return Response(
        png, media_type="image/png",
        headers={"Cache-Control": "no-store",
                 "Content-Disposition": f'inline; filename="{name}-{t:.3f}.png"'},
    )


EXPORT_CANCEL: dict[str, asyncio.Event] = {}
EXPORT_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | {
    ".mp3", ".wav", ".m4a", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf",
}


def exported_media(name: str) -> list[Path]:
    folder = require_project(name) / "exports"
    if not folder.exists():
        return []
    return sorted(
        (
            path for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in EXPORT_MEDIA_EXTENSIONS
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )


@app.get("/api/projects/{name}/exports")
def api_exports(name: str):
    files = []
    for path in exported_media(name):
        files.append({
            "name": path.name, "size": path.stat().st_size,
            "url": f"/media/{name}/exports/{path.name}",
            "download_url": f"/api/projects/{name}/exports/latest/download"
            if not files else f"/media/{name}/exports/{path.name}",
            "modified": path.stat().st_mtime,
            "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        })
    return {"files": files}


@app.get("/api/projects/{name}/exports/latest/download")
def api_download_latest_export(name: str):
    files = exported_media(name)
    if not files:
        raise HTTPException(404, "No exported media is available yet")
    latest = files[0]
    return FileResponse(
        latest,
        filename=latest.name,
        media_type=mimetypes.guess_type(latest.name)[0] or "application/octet-stream",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/projects/{name}/export")
async def api_start_export(name: str, req: Request):
    require_project(name)
    if name in EXPORT_CANCEL:
        raise HTTPException(409, "An export is already running for this project")
    transcription = TRANSCRIPTION_JOBS.get(name)
    if transcription and transcription.get("status") in {"queued", "loading", "transcribing", "canceling"}:
        raise HTTPException(409, "Wait for transcription to finish before exporting")
    body = await req.json()
    mode = body.get("mode", "lossless")
    resolution = body.get("resolution", "source")
    if mode not in {"lossless", "hq"}:
        raise HTTPException(400, "Unknown export mode")
    if resolution not in {"source", "1080", "720"}:
        raise HTTPException(400, "Unknown export resolution")
    asyncio.create_task(export(
        name, mode, resolution,
    ))
    return JSONResponse({"accepted": True}, status_code=202)


@app.post("/api/projects/{name}/export/cancel")
def api_cancel_export(name: str):
    require_project(name)
    event = EXPORT_CANCEL.get(name)
    if event:
        event.set()
    return {"ok": True, "running": bool(event)}


async def export(name: str, mode: str, resolution: str = "source"):
    if name in EXPORT_CANCEL:
        await progress(name, error="An export is already running for this project")
        return
    cancel = asyncio.Event()
    EXPORT_CANCEL[name] = cancel
    st = load_state(name)
    d = project_dir(name)
    exp = d / "exports"; exp.mkdir(exist_ok=True)
    log = exp / "export.log"
    log.write_text(
        f"Editoro {APP_VERSION} export started {datetime.now().isoformat()}\n"
        f"mode={mode} resolution={resolution} orientation={st.orientation}\n"
        f"look={json.dumps(look_state(st))} "
        f"defocus={st.look.defocus:.3f} grade={st.look.grade:.3f}\n",
        encoding="utf-8",
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = exp / f"{name}-{stamp}.mp4"
    src = d / st.source
    fps = st.source_info.fps or 30
    nvenc = nvenc_available()
    vcodec = video_encoder(st, nvenc)
    if not nvenc:
        await progress(name, note="NVENC unavailable - using the fast high-quality CPU encoder")
    work = exp / f"work-{stamp}"
    partial = work / "final.mp4"
    renderer: Optional[OverlayRenderSession] = None
    try:
        if not st.source or not src.is_file():
            raise ValueError("Import source footage before exporting")
        if mode not in {"lossless", "hq"}:
            raise ValueError("Unknown export mode")
        if resolution not in {"source", "1080", "720"}:
            raise ValueError("Unknown export resolution")
        if mode == "lossless":
            resolution = "source"
        segs = timeline_segments(st)
        if not segs:
            raise ValueError("The timeline is empty")
        expected_frames = plan_segment_frames(segs, fps)
        kfs = keyframes(src, log)
        parts: list[Path] = []
        work.mkdir()
        # A look changes every pixel of every frame, so there is no span left
        # that can be copied through untouched.
        copy_compatible = (
            st.source_info.codec in {"h264", "avc1", "hevc", "h265"}
            and not look_active(st)
        )
        annexb = bitstream_filter(st)

        async def encode_plain(seg_in: float, seg_out: float, target: Path) -> None:
            """Re-encode one untouched span, matched to the source parameters."""
            span = seg_out - seg_in
            count = max(1, int(round(span * fps)))
            matte = look_inputs(st, seg_in, span)
            command = [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{seg_in:.6f}", "-t", f"{span + 0.5:.6f}", "-i", str(src),
                *matte,
            ]
            filters = [f"fps={fps:.6f}"]
            if resolution != "source":
                target_w, target_h = export_dimensions(st, resolution)
                filters.append(f"scale={target_w}:{target_h}:flags=lanczos")
            filters.append("tpad=stop=-1:stop_mode=clone")
            # A span with nothing drawn on it still carries the look, so this
            # path builds the same chain the rendered spans do - otherwise the
            # grade would switch on and off at every graphic.
            graph = look_graph(st, "[0:v]", "[looked]", 1 if matte else None)
            if graph:
                command += ["-filter_complex",
                            f"{graph};[looked]{','.join(filters)}[v]", "-map", "[v]"]
            else:
                command += ["-vf", ",".join(filters)]
            command += [
                *vcodec, "-frames:v", str(count),
                "-an", *annexb, "-f", "mpegts", str(target),
            ]
            await run_cancelable(command, log, cancel, check=True)

        for idx, seg in enumerate(segs):
            if cancel.is_set():
                raise asyncio.CancelledError()
            await progress(name, seg=idx + 1, total=len(segs),
                           status="copy" if not seg["dirty"] and mode == "lossless" else "encode")
            part = work / f"p{idx:03d}.ts"
            if not seg["dirty"] and mode == "lossless" and copy_compatible:
                # Smart cut: a copied span has to start on a keyframe, so snap
                # forward to one and re-encode only the short slice in front.
                next_keyframe = min((k for k in kfs if k >= seg["src_in"] - 1e-3), default=None)
                if next_keyframe is None or next_keyframe >= seg["src_out"] - 1 / fps:
                    await encode_plain(seg["src_in"], seg["src_out"], part)
                elif next_keyframe - seg["src_in"] > 1 / fps:
                    lead = work / f"p{idx:03d}a.ts"
                    await encode_plain(seg["src_in"], min(next_keyframe, seg["src_out"]), lead)
                    parts.append(lead)
                    if next_keyframe >= seg["src_out"]:
                        continue
                    seg = {**seg, "src_in": next_keyframe}
                    await run_cancelable([
                        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{seg['src_in']:.6f}",
                        "-t", f"{seg['src_out'] - seg['src_in']:.6f}", "-i", str(src),
                        "-c:v", "copy", "-an", *annexb,
                        "-avoid_negative_ts", "make_zero", "-f", "mpegts", str(part),
                    ], log, cancel, check=True)
                else:
                    await run_cancelable([
                        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{seg['src_in']:.6f}",
                        "-t", f"{seg['src_out'] - seg['src_in']:.6f}", "-i", str(src),
                        "-c:v", "copy", "-an", *annexb,
                        "-avoid_negative_ts", "make_zero", "-f", "mpegts", str(part),
                    ], log, cancel, check=True)
            elif not seg["dirty"]:
                await encode_plain(seg["src_in"], seg["src_out"], part)
            else:
                if renderer is None:
                    overlay_w, overlay_h = export_dimensions(st, resolution)
                    # Built before the browser starts, so the page picks the
                    # scrub copies up in its own asset preload rather than
                    # loading the masters first and swapping later.
                    scrub = await asyncio.to_thread(
                        export_scrub_map, st, overlay_w, overlay_h, log)
                    renderer = OverlayRenderSession(
                        name, st, overlay_w, overlay_h, scrub)
                    await renderer.start()
                await encode_dirty_segment(
                    renderer, st, seg, src, part, vcodec, resolution, log, cancel,
                    name, idx + 1, len(segs),
                )
            parts.append(part)

        # Join. The spans are MPEG-TS rather than MP4 precisely because a copied
        # span and a re-encoded span carry different parameter sets; TS carries
        # those inline, so `-c copy` joins them instead of producing a file that
        # plays correctly only until the first join.
        await progress(name, seg=len(segs), total=len(segs), status="join")
        lst = work / "list.txt"
        lst.write_text("".join(f"file '{path.as_posix()}'\n" for path in parts), encoding="utf-8")
        pre = work / "video.mp4"
        await run_cancelable([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c:v", "copy", "-an", "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart", str(pre),
        ], log, cancel, check=True)

        expected_video_duration = sum(
            segment["src_out"] - segment["src_in"] for segment in segs
        )
        joined = probe(pre)
        joined_frames = count_frames(pre)
        # Frames, not seconds, are the thing that has to be right. A container
        # can round its duration; a missing frame silently shifts every later
        # cut against the audio, and that is the failure that is hard to see
        # until the whole video is finished.
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(
                f"\njoined {len(parts)} spans: {joined_frames} frames / {joined.duration:.3f}s "
                f"(expected {expected_frames} frames / {expected_video_duration:.3f}s)\n"
            )
        if joined_frames and abs(joined_frames - expected_frames) > 1:
            # Name the spans that disagree. "The video is 2 frames long" is not
            # something anyone can act on; "span 31 produced 8 of 6" is.
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("per-span frame audit (requested -> produced):\n")
                for index, (segment, path) in enumerate(zip(segs, parts)):
                    produced = count_frames(path)
                    wanted = segment_frames(segment, fps)
                    flag = "" if produced == wanted else "   <-- MISMATCH"
                    handle.write(
                        f"  {index:03d} {path.name} {wanted} -> {produced}{flag}\n"
                    )
            raise RuntimeError(
                "Video spans did not join accurately "
                f"({joined_frames} frames vs {expected_frames} expected); "
                "see the per-span frame audit in export.log"
            )

        # Build speech once from source cuts. Encoding audio in every tiny video
        # segment adds AAC priming at every join and causes duration drift.
        await progress(name, seg=len(segs), total=len(segs), status="audio")
        total_duration = expected_video_duration
        inputs, filters, amix = [str(pre)], [], []
        if st.source_info.audio_codec:
            speech = work / "speech.wav"
            cuts = st.cuts or [Cut(src_in=0, src_out=st.source_info.duration)]
            speech_filters = []
            speech_parts = []
            source_labels = ["[0:a]"]
            if len(cuts) > 1:
                source_labels = [f"[src{index}]" for index in range(len(cuts))]
                speech_filters.append(f"[0:a]asplit={len(cuts)}{''.join(source_labels)}")
            for index, cut in enumerate(cuts):
                speech_filters.append(
                    f"{source_labels[index if len(cuts) > 1 else 0]}"
                    f"atrim=start={cut.src_in}:end={cut.src_out},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
                speech_parts.append(f"[a{index}]")
            if len(speech_parts) == 1:
                speech_filters.append(f"{speech_parts[0]}aresample=48000[speech]")
            else:
                speech_filters.append(
                    f"{''.join(speech_parts)}concat=n={len(speech_parts)}:v=0:a=1,"
                    "aresample=48000[speech]"
                )
            await run_cancelable([
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src), "-filter_complex", ";".join(speech_filters),
                "-map", "[speech]", "-c:a", "pcm_s16le", str(speech),
            ], log, cancel, check=True)
            inputs.append(str(speech))
            amix.append("[1:a]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{total_duration}[base]")
            amix = ["[base]"]

        for i in st.instances:
            spec = TEMPLATE_PACKS.get(i.template, {})
            for event in resolve_sfx_events(i, spec):
                sound = event["path"]
                if not sound.exists():
                    continue
                inputs.append(str(sound))
                input_index = len(inputs) - 1
                # Sample-accurate rather than millisecond-rounded: a foley hit
                # that lands 4 ms late reads as sloppy, and the error was
                # previously different for every event in the timeline.
                delay_samples = max(0, int(round(event["time"] * 48000)))
                filters.append(
                    f"[{input_index}:a]aresample=48000,"
                    f"adelay=delays={delay_samples}S:all=1,"
                    f"volume={event['gain']:.4f}[s{input_index}]"
                )
                amix.append(f"[s{input_index}]")
            if not i.missing and i.fields.get("asset") and any(
                field.get("type") == "asset:video" for field in spec.get("fields", [])
            ):
                af = d / "assets" / i.fields["asset"]
                try:
                    has_clip_audio = af.exists() and bool(probe(af).audio_codec)
                except ValueError:
                    has_clip_audio = False
                if has_clip_audio:
                    inputs.append(str(af))
                    input_index = len(inputs) - 1
                    clip_in = float(i.fields.get("clip_in", 0) or 0)
                    delay_samples = max(0, int(round(i.start * 48000)))
                    filters.append(
                        f"[{input_index}:a]atrim={clip_in}:{clip_in + i.duration},"
                        f"asetpts=PTS-STARTPTS,aresample=48000,"
                        f"adelay=delays={delay_samples}S:all=1,"
                        f"volume={float(i.fields.get('volume', 0.8)):.4f}[s{input_index}]"
                    )
                    amix.append(f"[s{input_index}]")

        if len(amix) == 1:
            filters.append(f"{amix[0]}atrim=0:{total_duration},aresample=48000[mixed]")
        else:
            filters.append(
                f"{''.join(amix)}amix=inputs={len(amix)}:duration=first:normalize=0,"
                f"atrim=0:{total_duration},aresample=48000[mixed]"
            )
        # Speech plus foley can sum past full scale even when every part was
        # calibrated. A transparent brickwall costs nothing and is the
        # difference between a clean master and one that crackles on a phone.
        filters.append(
            "[mixed]alimiter=limit=0.97:attack=1.5:release=60:level=disabled,"
            "aresample=48000:first_pts=0[a]"
        )
        fc = ";".join(filters)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
        for x in inputs:
            cmd += ["-i", x]
        cmd += [
            "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
            "-movflags", "+faststart", str(partial),
        ]
        await run_cancelable(cmd, log, cancel, check=True)
        verified = probe(partial)
        if not verified.codec or verified.duration <= 0:
            raise RuntimeError("FFmpeg created an invalid output file")
        final_frames = count_frames(partial)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(
                f"final: {final_frames} frames / {verified.duration:.3f}s "
                f"(expected {expected_frames} / {total_duration:.3f}s)\n"
            )
        if final_frames and abs(final_frames - expected_frames) > 1:
            raise RuntimeError(
                f"Final video lost frames ({final_frames} vs {expected_frames} expected)"
            )
        # The cover, if one was asked for. After the audit, so a deliberately
        # added still can never be mistaken for a lost or duplicated frame.
        await prepend_thumbnail(st, name, partial, work, fps, vcodec, log, cancel)
        verified = probe(partial)
        os.replace(partial, final)
        shutil.rmtree(work, ignore_errors=True)
        await progress(name, done=True, file=f"exports/{final.name}",
                       duration=verified.duration, codec=verified.codec, nvenc=nvenc)
    except asyncio.CancelledError:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("\nEXPORT CANCELED\n")
        shutil.rmtree(work, ignore_errors=True)
        final.unlink(missing_ok=True)
        await progress(name, canceled=True)
    except Exception as e:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\nEXPORT FAILED: {e}\n")
        await progress(name, error=str(e))
    finally:
        if renderer is not None:
            try:
                await renderer.close()
            except Exception as close_error:
                with open(log, "a", encoding="utf-8") as handle:
                    handle.write(f"\nRenderer cleanup warning: {close_error}\n")
        EXPORT_CANCEL.pop(name, None)


def _test_compositing() -> None:
    """The plane split: which planes a segment asks for, and the graph shape."""
    scan_templates()
    base = ProjectState(name="t", source_info=MediaInfo(duration=10, width=1920, height=1080))
    seg = {"src_in": 0.0, "src_out": 2.0, "tl": 0.0, "camera": None}

    # Nothing special on the timeline: one plane, and therefore the exact graph
    # Editoro built before any of this existed.
    plain = base.model_copy(update={"instances": [
        Instance(id="a", template="keyword", start=0, duration=2)]})
    assert overlay_slots(plain, seg) == ["front"], overlay_slots(plain, seg)
    graph = overlay_composite_graph(["front"], "[padded]", "[1:v]", "[out]", 1920, 1080, None)
    assert "crop=" not in graph and "gblur" not in graph, graph
    assert graph.count("overlay=") == 1, graph

    # A block asking for the frost adds the plane it is composited over and the
    # mask that fades it, and nothing else.
    frosted = base.model_copy(update={"instances": [
        Instance(id="a", template="keyword", start=0, duration=2),
        Instance(id="b", template="backdrop-blur", start=0, duration=2, backdrop=True)]})
    assert overlay_slots(frosted, seg) == ["below", "mask", "front"]
    graph = overlay_composite_graph(
        ["below", "mask", "front"], "[padded]", "[1:v]", "[out]", 1920, 1080, None)
    assert "split=3" in graph and graph.count("crop=") == 3, graph
    # The three cells are cropped from the strip in the order they were named.
    for index, slot in enumerate(["below", "mask", "front"]):
        assert f"crop=1920:1080:{index * 1920}:0[ov{slot}]" in graph, (slot, graph)
    assert "alphaextract" in graph and "alphamerge" in graph, graph
    assert f"gblur=sigma={BACKDROP_BLUR * 1080:.3f}" in graph, graph
    assert graph.endswith("[out]"), graph

    # Behind-the-speaker needs a matte; without one the plane is dropped rather
    # than producing a graph that refers to an input that is not there.
    behind = base.model_copy(update={"instances": [
        Instance(id="a", template="keyword", start=0, duration=2, depth="behind")]})
    assert overlay_slots(behind, seg) == ["front"], "no matte means no behind plane"
    graph = overlay_composite_graph(
        ["behind", "front"], "[padded]", "[1:v]", "[out]", 1920, 1080, "[mt]")
    assert "[dpsubject][dpmask]alphamerge" in graph, graph
    # The speaker goes back on AFTER the block that was put behind them.
    assert graph.index("[ovbehind]overlay") < graph.index("[dpcut]overlay"), graph

    # A block only counts for a segment it is actually on screen during.
    late = base.model_copy(update={"instances": [
        Instance(id="a", template="backdrop-blur", start=5, duration=2, backdrop=True)]})
    assert overlay_slots(late, seg) == ["front"]

    # The camera move is reproducible onto another stream at another size,
    # which is what keeps a matte lined up with the footage under a punch-in.
    assert camera_filter_graph(plain, seg, 30, "[m]", "[mc]", (960, 540)).endswith("[mc]")


def _test_breathe() -> None:
    """The breathing zoom: its amplitude, its overrides, and its cost."""
    state = ProjectState(
        name="breathe-test", breathe="standard",
        source_info=MediaInfo(width=1920, height=1080, fps=30, duration=30),
    )
    assert state.orientation == "horizontal"
    assert breathe_level("standard", "horizontal") == 0.022
    # Vertical breathes deeper for the same named strength, because the same
    # percentage of a smaller frame reads as less movement.
    assert breathe_level("standard", "vertical") > breathe_level("standard", "horizontal")
    assert breathes(state) and not breathes(state.model_copy(update={"breathe": "off"}))

    # A camera block is an override to zero, so nothing stacks on a punch-in.
    state.instances = [Instance(id="p", template="punch-in", start=5, duration=3)]
    assert (5.0, 8.0, 0.0, 0.5, 0.5) in breathe_spans(state)
    # ...and a project with the effect off but a `breathe` block asking for it
    # is still breathing, or the block would silently do nothing.
    quiet = ProjectState(
        name="breathe-test", breathe="off",
        source_info=MediaInfo(width=1920, height=1080, fps=30, duration=30),
        instances=[Instance(id="b", template="breathe", start=0, duration=4,
                            fields={"strength": "strong"})],
    )
    assert breathes(quiet)
    assert breathe_spans(quiet)[0][2] == breathe_level("strong", "horizontal")

    # The expression is evaluatable arithmetic, not a shape that happens to
    # look right: FFmpeg will not tell us if it is unbalanced.
    expression = breathe_amplitude_expr(state, "T")
    assert expression.count("(") == expression.count(")")
    assert f"{breathe_level('standard', 'horizontal'):.9f}" in expression and "clip(" in expression

    # The block's rectangle is a limit on top of the strength, not a second
    # strength: the default box is far wider than any strength asks for, so it
    # changes nothing, and the centre is the middle of whatever box is drawn.
    pack_default = TEMPLATE_PACKS["breathe"]["fields"][1]["default"]
    roomy = Instance(id="b", template="breathe", start=0, duration=4,
                     fields={"strength": "strong", "rect": pack_default})
    assert breathe_framing(state, roomy)[0] == breathe_level("strong", "horizontal")
    off_centre = roomy.model_copy(update={"fields": {
        "strength": "strong", "rect": {"x": .5, "y": .1, "w": .4, "h": .5}}})
    depth, cx, cy = breathe_framing(state, off_centre)
    assert (cx, cy) == (0.7, 0.35)
    # w=.4 caps the zoom at 1/.4, which is far beyond `strong`, so the tighter
    # edge - h=.5 - is the one that binds, and it does not bind either.
    assert depth == breathe_level("strong", "horizontal")
    tight = roomy.model_copy(update={"fields": {"strength": "strong",
                                                "rect": {"x": .01, "y": .01, "w": .98, "h": .98}}})
    assert breathe_framing(state, tight)[0] < breathe_level("strong", "horizontal")
    # A rect that is not a rect is ignored rather than crashing an export.
    for junk in (None, "middle", {"w": "wide"}):
        assert breathe_framing(state, roomy.model_copy(
            update={"fields": {"strength": "strong", "rect": junk}}))[1] == 0.5

    # The centre expression is a real expression, and it is 0.5 flat when
    # nothing has asked for anything else.
    centred = breathe_centre_expr(state, "T", "x")
    assert centred.count("(") == centred.count(")") and "0.500000000" in centred

    graph = breathe_filter_graph(state, {"tl": 0.0}, 30, "[0:v]", "[base]")
    # zoompan is what made this wobble; see the note in breathe_filter_graph().
    assert "zoompan" not in graph
    assert "perspective=" in graph and "sin(" in graph and "eval=frame" in graph
    assert graph.count("(") == graph.count(")")
    off = state.model_copy(update={"breathe": "off", "instances": []})
    assert breathe_filter_graph(off, {"tl": 0.0}, 30, "[0:v]", "[base]") \
        == "[0:v]setpts=PTS-STARTPTS[base]", "off must be genuinely off"
    print("breathing tests OK")


def _test_camera_modes() -> None:
    """Each camera mode does what its pack says, at the two ends of its span.

    These are the assertions the preview used to fail and the export used to
    pass: ken-burns travelling instead of snapping, and zoom-out opening out
    rather than closing in. cameraStateAt() in index.html evaluates the same
    four cases, and tools/test_ui.py compares the two at sampled times.
    """
    fps = 30.0

    def zoom_at(item: Instance, when: float) -> float:
        plan = camera_plan(ProjectState(name="camera-test"), item, fps)
        expression, _, _ = camera_expressions(plan, f"{when:.6f}")
        # The expressions are plain arithmetic over a literal time, so they
        # can be evaluated directly. `if` is renamed because it is a keyword
        # here and a function there; both branches evaluating eagerly is
        # harmless when every denominator in them is a clamped window.
        return eval(expression.replace("if(", "_if("), {  # noqa: S307
            "clip": lambda value, low, high: max(low, min(high, value)),
            "pow": pow, "min": min, "max": max,
            "_if": lambda condition, a, b=0: a if condition else b,
            "lt": lambda a, b: a < b, "gt": lambda a, b: a > b,
        })

    tight = {"x": 0.22, "y": 0.16, "w": 0.56, "h": 0.64}
    wide = {"x": 0.1, "y": 0.08, "w": 0.8, "h": 0.84}

    punch = Instance(id="p", template="punch-in", start=0, duration=3, fields={"rect": tight})
    assert zoom_at(punch, 0.0) < 1.02, "a punch-in starts on the full frame"
    assert zoom_at(punch, 1.5) > 1.4, "a punch-in holds tight in the middle"

    out = Instance(id="z", template="zoom-out", start=0, duration=3, fields={"rect": tight})
    assert zoom_at(out, 0.0) > 1.4, "a zoom-out starts tight"
    assert zoom_at(out, 2.9) < 1.05, "a zoom-out ends on the full frame"

    burns = Instance(id="k", template="ken-burns", start=0, duration=4,
                     fields={"rect": wide, "rect2": tight})
    begin, middle, end = zoom_at(burns, 0.0), zoom_at(burns, 2.0), zoom_at(burns, 3.99)
    assert begin < middle < end, "ken-burns travels; it does not snap and hold"
    assert abs(begin - 1 / 0.84) < 0.02, "ken-burns begins on its start framing"
    print("camera tests OK")


def _test_value_ladder() -> None:
    """The counting ladder, and the foley that has to land on it.

    The point of the whole mechanism is that the ticks and the digits are the
    same list of times, so these assertions are about the shape of that list:
    it must start at the beginning of the climb, end exactly on the arrival,
    and decelerate the whole way without ever being too fast to hear.
    """
    spec = TEMPLATE_PACKS["stat-pop"]
    item = Instance(id="s1", template="stat-pop", start=2.0, duration=3.0,
                    fields={"value": "250"})
    delay, window, value = value_ramp(spec, item, "number")
    # "number" is the second staggered element, so it starts one 70 ms step in.
    assert abs(delay - 0.07) < 1e-9, delay
    assert abs(window - 1.0) < 1e-9, window
    assert value["steps"] == 10

    times = value_step_times(spec, item, "number")
    assert len(times) == 10, times
    assert times[0] > delay, times[0]
    # The last notch is the arrival: the final tick and the final digit are the
    # same instant, which is the only moment in the effect the ear checks.
    assert abs(times[-1] - (delay + window)) < 1e-9, times[-1]
    gaps = [b - a for a, b in zip([delay] + times, times)]
    assert all(b > a for a, b in zip(gaps, gaps[1:])), gaps
    # Fast enough at the top to read as a mechanism, never so fast that two
    # 45 ms wooden ticks overlap into a buzz; slow enough at the bottom to feel
    # like it is settling rather than stopping dead.
    assert 0.045 <= gaps[0] <= 0.06, gaps[0]
    assert 0.14 <= gaps[-1] <= 0.20, gaps[-1]

    events = resolve_sfx_events(item, spec)
    ticks = [e for e in events if "count-wood" in e["file"]]
    assert len(ticks) == 10, len(ticks)
    assert ticks[0]["time"] > item.start + delay
    assert abs(ticks[-1]["time"] - (item.start + delay + window)) < 1e-9
    # The old sound named a repeat_spacing but no repeat_field, so it fired
    # exactly once: a "counting" sound that made a single click.
    assert not any("count-tick" in e["file"] for e in events)

    # Silence still wins over the ladder, and `lite` - the default - is the
    # count without the pop that opens the block.
    item.foley = "off"
    assert resolve_sfx_events(item, spec) == []
    item.foley = "full"
    assert any("pop" in e["file"] for e in resolve_sfx_events(item, spec))
    item.foley = "lite"
    assert not any("pop" in e["file"] for e in resolve_sfx_events(item, spec))
    assert any("count-wood" in e["file"] for e in resolve_sfx_events(item, spec))
    # A project written before foley had three states still loads.
    assert Instance(id="s9", template="stat-pop", start=0.0, duration=1.0,
                    silent=True).foley == "off"

    # A short block shortens the climb, and the ticks follow it down rather
    # than running past the end of the block.
    brief = Instance(id="s2", template="stat-pop", start=0.0, duration=1.0,
                     fields={"value": "9"})
    short = value_step_times(spec, brief, "number")
    assert short[-1] < brief.duration, short[-1]
    assert all(e["time"] <= brief.duration for e in resolve_sfx_events(brief, spec))

    # "Increase by" hands the notch count to the block. 1000 counted up in
    # 200s is five notches, so the digits land on 200, 400, 600, 800, 1000 -
    # numbers a viewer recognises - and there are five ticks, not ten.
    counting = Instance(id="s3", template="stat-pop", start=0.0, duration=3.0,
                        fields={"value": "1,000", "start": "0", "step": "200"})
    assert value_steps(spec, counting) == 5
    assert len(value_step_times(spec, counting, "number")) == 5
    assert len([e for e in resolve_sfx_events(counting, spec) if "count-wood" in e["file"]]) == 5
    # Counting down, and counting from something other than zero, are the same
    # arithmetic: it is the distance that decides how many increments there are.
    downward = counting.model_copy(update={"fields": {"value": "40", "start": "100", "step": "20"}})
    assert value_steps(spec, downward) == 3
    # An empty or nonsensical "increase by" falls back to the pack's own count
    # rather than to a ladder with no rungs.
    for junk in ("", "0", "abc"):
        blank = counting.model_copy(update={"fields": {"value": "1000", "step": junk}})
        assert value_steps(spec, blank) == 10, junk
    # More increments than the ladder can hold is clamped, not refused: a
    # 64-notch count is already faster than the ear can separate.
    dense = counting.model_copy(update={"fields": {"value": "1000", "step": "1"}})
    assert value_steps(spec, dense) == 64

    # A ratio of 1 is an even ladder, which is what a pack gets if it asks for
    # steps and says nothing about the deceleration.
    even = dict(spec, motion={**spec["motion"], "value": {"ms": 1000, "steps": 4, "ratio": 1.0}})
    flat = value_step_times(even, item, "number")
    spacing = [round(b - a, 9) for a, b in zip([delay] + flat, flat)]
    assert len(set(spacing)) == 1, spacing
    print("counting tests OK")


def run_self_tests() -> None:
    """Fast deterministic checks used by launch-independent verification."""
    _test_parser()
    _test_look()
    _test_compositing()
    _test_breathe()
    _test_camera_modes()
    _test_value_ladder()
    assert len(TEMPLATE_PACKS) >= 30, sorted(TEMPLATE_PACKS)
    assert not TEMPLATE_ERRORS, TEMPLATE_ERRORS
    colours: dict[str, str] = {}
    for spec in TEMPLATE_PACKS.values():
        folder = TEMPLATES_DIR / spec["_dir"]
        assert (folder / "render.js").is_file(), f"{spec['id']} has no renderer"
        assert spec["_has_render"] is True
        assert spec.get("description"), f"{spec['id']} has no description for agents"
        for filename in spec.get("assets", []):
            assert (folder / filename).is_file(), (spec["id"], filename)
        # Both orientations are mandatory: a template that only knows 16:9 would
        # silently fall back to a 16:9 layout on a vertical cut.
        for orientation in ORIENTATIONS:
            variant = spec["_variants"][orientation]
            assert 0 <= variant["x"] <= 1 and 0 <= variant["y"] <= 1, (spec["id"], orientation)
        colour = spec["category_color"].upper()
        assert colour not in colours, (spec["id"], colours.get(colour))
        colours[colour] = spec["id"]
        for sound in spec.get("sfx", []):
            assert sfx_file(spec, sound["file"]).is_file(), (spec["id"], sound["file"])
        for field in spec["fields"]:
            assert field.get("description") or field.get("note") or field["type"] == "none", \
                f"{spec['id']}.{field['name']} has no description for agents"
    assert not TEMPLATE_PACKS["punch-in"].get("sfx")
    assert not TEMPLATE_PACKS["zoom-out"].get("sfx")
    assert camera_packs() >= {"punch-in", "zoom-out", "ken-burns"}

    # A malformed motion block must fail loudly at scan time rather than render
    # as something subtly wrong in an export three hours later.
    for broken in (
        {"in": {"type": "bounce"}},
        {"in": {"type": "ease", "from": {"wobble": 1}}},
        {"shadow": {"layers": 99}},
        {"blur": {"max": 0}},
    ):
        try:
            _check_motion(broken)
            raise AssertionError(f"invalid motion accepted: {broken}")
        except ValueError:
            pass
    _check_motion({"in": {"type": "spring", "stiffness": 200, "damping": 18, "ms": 500,
                          "from": {"opacity": 0, "scale": .6, "y": -40, "rotate": -5}},
                   "stagger": {"step_ms": 60, "elements": ["card", "text"]},
                   "shadow": {"layers": 3}, "blur": {"max": 6}})
    try:
        _check_variants({"variants": {"horizontal": {"x": 0, "y": 0, "scale": 1}}})
        raise AssertionError("a template missing its 9:16 variant was accepted")
    except ValueError:
        pass

    srt = "1\n00:00:00,000 --> 00:00:01,500\nHello world\n\n2\n00:00:01,500 --> 00:00:03,000\nSecond line\n"
    parsed_srt = parse_transcript(srt, "captions.srt")
    assert [block.text for block in parsed_srt] == ["Hello world", "Second line"]
    whisper = json.dumps({"segments": [{"words": [
        {"word": "one", "start": 0.0, "end": 0.4},
        {"word": "two", "start": 0.5, "end": 0.9},
        {"word": "three.", "start": 1.0, "end": 1.4},
        {"word": "next", "start": 1.5, "end": 2.0},
    ]}]})
    parsed_json = parse_transcript(whisper, "captions.json")
    assert len(parsed_json) == 2 and parsed_json[0].words[0].w == "one"
    grouped = group_caption_words([
        CaptionWord(w="Hello", s=0.0, e=0.4, p=.97),
        CaptionWord(w="world.", s=0.45, e=0.9, p=.93),
        CaptionWord(w="After", s=1.0, e=1.3, p=.91),
        CaptionWord(w="pause", s=2.1, e=2.5, p=.88),
    ])
    assert [block.text for block in grouped] == ["Hello world.", "After", "pause"]
    assert grouped[0].words[0].p == .97

    foley_item = Instance(
        id="marker", template="highlight", start=3, duration=2,
        fields={"strokes": [{}, {}, {}]},
    )
    foley = resolve_sfx_events(foley_item, {
        "sfx": [{
            "file": "marker.wav", "gain": .2, "offset_ratio": .1,
            "repeat_field": "strokes", "spread_ratio": .6,
        }]
    })
    assert [round(event["time"], 2) for event in foley] == [3.2, 3.8, 4.4]
    assert all(event["gain"] == .2 for event in foley)

    # A repeating sound counts lists, text lines and numbers alike, so a pack
    # never needs a hidden field just to make its foley match what is drawn.
    counting = Instance(id="c", template="checklist", start=0, duration=4, fields={
        "items": "first\nsecond\nthird", "strokes": [{}, {}], "from": 4,
    })
    assert sfx_repeat_count(counting, "items") == 3
    assert sfx_repeat_count(counting, "strokes") == 2
    assert sfx_repeat_count(counting, "from") == 4
    assert sfx_repeat_count(counting, "missing") == 1
    assert sfx_repeat_count(counting, "") == 1

    sentence_words = [
        CaptionWord(w="One", s=0.0, e=0.3), CaptionWord(w="two", s=0.3, e=0.6),
        CaptionWord(w="three", s=0.6, e=0.9), CaptionWord(w="four.", s=0.9, e=1.2),
        CaptionWord(w="Next", s=1.3, e=1.6), CaptionWord(w="one.", s=1.6, e=1.9),
    ]
    assert len(group_caption_words(sentence_words, mode="sentence")) == 2
    assert len(group_caption_words(sentence_words, mode="chunk", words_per_chunk=2)) == 3

    # Orientation is derived from the footage unless it is explicitly pinned.
    portrait = ProjectState(name="p", source_info=MediaInfo(width=1080, height=1920, duration=5))
    assert portrait.orientation == "vertical"
    landscape = ProjectState(name="l", source_info=MediaInfo(width=1920, height=1080, duration=5))
    assert landscape.orientation == "horizontal"
    pinned = ProjectState(
        name="v", orientation_mode="vertical",
        source_info=MediaInfo(width=1920, height=1080, duration=5),
    )
    assert pinned.orientation == "vertical"
    layered = ProjectState(
        name="k", source_info=MediaInfo(width=1920, height=1080, fps=30, duration=8),
        instances=[Instance(id="i", template="keyword", start=1, duration=2,
                            x=.4, y=.2, scale=.8)],
    )
    assert layered.instances[0].layouts["horizontal"].x == .4

    # A caption dragged off the global placement keeps its own coordinates.
    pinned_caption = CaptionBlock(id="c1", start=0, end=1, text="x",
                                  follow_global=False, x=.3, y=.7)
    assert pinned_caption.follow_global is False and pinned_caption.y == .7

    # ...and it keeps them per orientation, so pinning one in 16:9 leaves the
    # 9:16 placement alone.
    both = ProjectState(
        name="c", source_info=MediaInfo(width=1920, height=1080, fps=30, duration=8),
        captions=[CaptionBlock(id="c1", start=0, end=1, text="x",
                               follow_global=False, x=.3, y=.7)],
    )
    assert both.captions[0].layouts["horizontal"].y == .7
    assert "vertical" not in both.captions[0].layouts
    following = ProjectState(
        name="c", source_info=MediaInfo(width=1920, height=1080, fps=30, duration=8),
        captions=[CaptionBlock(id="c1", start=0, end=1, text="x")],
    )
    assert not following.captions[0].layouts

    sample = ProjectState(
        name="selftest", source_info=MediaInfo(duration=10), breathe="off",
        cuts=[Cut(src_in=0, src_out=10)],
        instances=[Instance(id="x", template="keyword", start=2, duration=2)],
    )
    pieces = timeline_segments(sample)
    assert [(p["src_in"], p["src_out"], p["dirty"]) for p in pieces] == [
        (0.0, 2.0, False), (2.0, 4.0, True), (4.0, 10.0, False)
    ]
    # With breathing on there is no clean span to copy, because the footage is
    # moving everywhere. That is the cost of the effect and it has to be
    # visible in the classifier rather than discovered at export.
    breathing = sample.model_copy(update={"breathe": "standard"})
    assert all(piece["dirty"] for piece in timeline_segments(breathing))
    try:
        project_dir("../escape")
        raise AssertionError("path traversal was accepted")
    except ValueError:
        pass
    try:
        ProjectState(
            name="overlap", source_info=MediaInfo(duration=10),
            cuts=[Cut(src_in=0, src_out=6), Cut(src_in=5, src_out=8)],
        )
        raise AssertionError("overlapping source cuts were accepted")
    except ValueError:
        pass
    punch = Instance(
        id="punch", template="punch-in", start=1, duration=2,
        fields={"rect": {"x": .25, "y": .25, "w": .5, "h": .5}},
    )
    punch_state = ProjectState(
        name="punch", source_info=MediaInfo(width=640, height=360, fps=30, duration=4),
        cuts=[Cut(src_in=0, src_out=4)], instances=[punch],
    )
    graph = punch_filter_graph(
        punch_state,
        {"src_in": 1.0, "src_out": 2.0, "tl": 1.0, "dirty": True, "camera": "punch"},
        30,
    )
    assert "zoompan" in graph and "1.000000000" in graph
    zoom_out = Instance(
        id="zoom-out", template="zoom-out", start=1, duration=2,
        fields={"rect": {"x": .2, "y": .2, "w": .6, "h": .6}},
    )
    zoom_out_state = punch_state.model_copy(update={"instances": [zoom_out]})
    zoom_out_graph = camera_filter_graph(
        zoom_out_state,
        {"src_in": 1.0, "src_out": 2.0, "tl": 1.0, "dirty": True, "camera": "zoom-out"},
        30,
    )
    assert "zoompan" in zoom_out_graph and "1-" in zoom_out_graph

    # Every camera mode must produce a usable graph; a mode that silently fell
    # back to "no transform" would make the export quietly disagree with the
    # preview, which is the single worst failure this system can have.
    for template_id in sorted(camera_packs()):
        spec = TEMPLATE_PACKS[template_id]
        camera_instance = Instance(
            id=f"cam-{template_id}", template=template_id, start=1, duration=2,
            fields={
                "rect": {"x": .2, "y": .2, "w": .5, "h": .5},
                "rect2": {"x": .3, "y": .25, "w": .4, "h": .45},
            },
        )
        camera_state = punch_state.model_copy(update={"instances": [camera_instance]})
        built = camera_filter_graph(
            camera_state,
            {"src_in": 1.0, "src_out": 2.0, "tl": 1.0, "dirty": True,
             "camera": camera_instance.id},
            30,
        )
        assert "zoompan" in built, (template_id, spec["camera"]["mode"], built)
        assert "[base]" in built

    drift = camera_plan(
        punch_state.model_copy(update={"instances": [Instance(
            id="kb", template="ken-burns", start=0, duration=4,
            fields={"rect": {"x": .1, "y": .1, "w": .8, "h": .8},
                    "rect2": {"x": .3, "y": .3, "w": .4, "h": .4}})]}),
        Instance(id="kb", template="ken-burns", start=0, duration=4,
                 fields={"rect": {"x": .1, "y": .1, "w": .8, "h": .8},
                         "rect2": {"x": .3, "y": .3, "w": .4, "h": .4}}),
        30,
    )
    assert drift["mode"] == "drift"
    assert drift["second"]["zoom"] > drift["first"]["zoom"]

    assert segment_frames({"src_in": 0.0, "src_out": 2.0}, 30) == 60
    assert segment_frames({"src_in": 1.0, "src_out": 1.0166667}, 60) == 1
    assert "h264_mp4toannexb" in bitstream_filter(
        ProjectState(name="b", source_info=MediaInfo(codec="h264")))
    assert "hevc_mp4toannexb" in bitstream_filter(
        ProjectState(name="b", source_info=MediaInfo(codec="hevc")))
    matched = video_encoder(
        ProjectState(name="e", source_info=MediaInfo(codec="h264", profile="High",
                                                     pix_fmt="yuv420p")),
        nvenc=False,
    )
    assert "-profile:v" in matched and "high" in matched

    edit = ProjectState(
        name="tl", source_info=MediaInfo(width=640, height=360, fps=30, duration=12),
        cuts=[Cut(src_in=0, src_out=4), Cut(src_in=8, src_out=12)],
    )
    assert abs(timeline_duration(edit) - 8.0) < 1e-6
    assert abs(timeline_to_source(edit, 1.0) - 1.0) < 1e-6
    assert abs(timeline_to_source(edit, 5.0) - 9.0) < 1e-6

    async def cancellation_check():
        event = asyncio.Event()

        async def trigger_cancel():
            await asyncio.sleep(.15)
            event.set()

        trigger = asyncio.create_task(trigger_cancel())
        started = time.monotonic()
        try:
            await run_cancelable(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                None, event, check=True,
            )
            raise AssertionError("a canceled subprocess was allowed to finish")
        except asyncio.CancelledError:
            assert time.monotonic() - started < 5
        finally:
            await trigger

    asyncio.run(cancellation_check())

    import httpx
    test_name = "editoro-selftest"
    test_folder = project_dir(test_name)
    if test_folder.exists():
        shutil.rmtree(test_folder)

    async def api_checks():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/health")).json()["templates"] >= 30
            catalogue = (await client.get("/api/templates")).json()
            assert len(catalogue["packs"]) >= 30
            assert any(name.startswith("art/") for name in catalogue["shared"])
            assert any(pack.get("camera") for pack in catalogue["packs"])
            assert (await client.get("/api/projects/does-not-exist/state")).status_code == 404
            assert (await client.get("/api/projects/does-not-exist/assets")).status_code == 404
            assert (await client.get("/api/projects/does-not-exist/directives")).status_code == 404
            assert (await client.post(f"/api/projects/{test_name}")).status_code == 200
            bad_export = await client.post(
                f"/api/projects/{test_name}/export",
                json={"mode": "invalid", "resolution": "source"},
            )
            assert bad_export.status_code == 400
            transcription_status = await client.get(
                f"/api/projects/{test_name}/transcribe"
            )
            assert transcription_status.status_code == 200
            assert transcription_status.json()["status"] == "idle"
            no_source = await client.post(
                f"/api/projects/{test_name}/transcribe",
                json={"model": "large-v3", "language": "auto", "terms": ""},
            )
            assert no_source.status_code == 409
            canceled_idle = await client.post(
                f"/api/projects/{test_name}/transcribe/cancel"
            )
            assert canceled_idle.status_code == 200
            assert canceled_idle.json()["status"] == "idle"
            empty_download = await client.get(
                f"/api/projects/{test_name}/exports/latest/download"
            )
            assert empty_download.status_code == 404
            exported = test_folder / "exports" / "latest.mp4"
            exported.write_bytes(b"test-export")
            latest = await client.get(
                f"/api/projects/{test_name}/exports/latest/download"
            )
            assert latest.status_code == 200 and latest.content == b"test-export"
            assert "attachment" in latest.headers.get("content-disposition", "")
            media_file = test_folder / "assets" / "range.bin"
            media_file.write_bytes(b"0123456789")
            ranged = await client.get(
                f"/media/{test_name}/assets/range.bin",
                headers={"Range": "bytes=2-5"},
            )
            assert ranged.status_code == 206 and ranged.content == b"2345"
            suffix = await client.get(
                f"/media/{test_name}/assets/range.bin",
                headers={"Range": "bytes=-3"},
            )
            assert suffix.status_code == 206 and suffix.content == b"789"
            invalid = await client.get(
                f"/media/{test_name}/assets/range.bin",
                headers={"Range": "bytes=99-"},
            )
            assert invalid.status_code == 416
            transcript = await client.post(
                f"/api/projects/{test_name}/transcript",
                files={"file": ("captions.srt", srt.encode("utf-8"), "application/x-subrip")},
            )
            assert transcript.status_code == 200 and len(transcript.json()["captions"]) == 2

            words_json = json.dumps({"segments": [{"words": [
                {"word": "alpha", "start": 0.0, "end": 0.4},
                {"word": "beta", "start": 0.4, "end": 0.8},
                {"word": "gamma.", "start": 0.8, "end": 1.2},
                {"word": "delta", "start": 1.4, "end": 1.8},
                {"word": "epsilon.", "start": 1.8, "end": 2.2},
            ]}]})
            imported = await client.post(
                f"/api/projects/{test_name}/transcript",
                files={"file": ("captions.json", words_json.encode("utf-8"), "application/json")},
            )
            assert imported.status_code == 200
            readable = await client.get(f"/api/projects/{test_name}/transcript")
            assert readable.status_code == 200
            assert len(readable.json()["words"]) == 5
            assert "alpha" in readable.json()["text"]
            regrouped = await client.post(
                f"/api/projects/{test_name}/captions/regroup",
                json={"mode": "sentence", "words_per_chunk": 5},
            )
            assert regrouped.status_code == 200
            assert len(regrouped.json()["captions"]) == 2
            rechunked = await client.post(
                f"/api/projects/{test_name}/captions/regroup",
                json={"mode": "chunk", "words_per_chunk": 2},
            )
            assert len(rechunked.json()["captions"]) == 3

            # The look endpoints: the two amounts round-trip, the grade table
            # is served in the form the preview uploads, and a project with
            # nothing analysed reports that rather than failing.
            look = await client.get(f"/api/projects/{test_name}/look")
            assert look.status_code == 200
            assert look.json()["defocus"] == 0 and look.json()["grade"] == 0
            assert look.json()["matte_ready"] is False
            assert look.json()["running"] is False
            assert (await client.get(f"/api/projects/{test_name}/look/lut")).status_code == 404
            changed = await client.post(
                f"/api/projects/{test_name}/look", json={"defocus": 0.5, "grade": 0.8})
            assert changed.status_code == 200, changed.text
            assert changed.json()["defocus"] == 0.5 and changed.json()["grade"] == 0.8
            assert load_state(test_name).look.defocus == 0.5
            # Amounts alone change nothing on screen: without a matte and a
            # measured grade there is nothing to apply.
            assert not look_active(load_state(test_name))
            refused = await client.post(
                f"/api/projects/{test_name}/look", json={"defocus": 4})
            assert refused.status_code == 422
            bad_parts = await client.post(
                f"/api/projects/{test_name}/look/analyze", json={"parts": ["sharpen"]})
            assert bad_parts.status_code == 400, bad_parts.text

            missing_asset = await client.get(
                f"/api/projects/{test_name}/asset-info", params={"asset": "nope.png"})
            assert missing_asset.status_code == 404
            escape = await client.get(
                f"/api/projects/{test_name}/asset-info",
                params={"asset": "../../server.py"})
            assert escape.status_code == 404

    try:
        asyncio.run(api_checks())
    finally:
        if test_folder.exists():
            shutil.rmtree(test_folder)
    print("backend/API tests OK")


def run_export_e2e_tests() -> None:
    """Exercise cuts, audio, all template packs, and both export modes."""
    from urllib.request import urlopen

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and FFprobe are required for end-to-end tests")
    if not renderer_executable():
        raise RuntimeError("Playwright Chromium is required; run launch.cmd once")

    def health() -> Optional[dict]:
        try:
            with urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=.5) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

    current_health = health()
    if current_health and current_health.get("app") != "Editoro":
        raise RuntimeError(f"Port {PORT} is already used by another application")
    if not current_health:
        thread = threading.Thread(
            target=lambda: uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error"),
            daemon=True,
        )
        thread.start()
        for _ in range(80):
            if health():
                break
            time.sleep(.1)
        else:
            raise RuntimeError("The test server did not start")

    name = f"editoro-e2e-{uuid.uuid4().hex[:8]}"
    folder = project_dir(name)
    folder.mkdir(parents=True)
    (folder / "assets").mkdir()
    (folder / "exports").mkdir()
    try:
        source = folder / "source.mp4"
        run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=12",
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=12",
            "-c:v", "libx264", "-preset", "veryfast", "-g", "300",
            "-keyint_min", "300", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(source),
        ], check=True)
        asset = folder / "assets" / "card.png"
        run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x4ECDC4:s=480x320",
            "-frames:v", "1", str(asset),
        ], check=True)
        inserted_video = folder / "assets" / "insert.mp4"
        shutil.copy2(source, inserted_video)
        info = probe(source)
        # One instance of every installed pack. This is the regression net that
        # matters: a renderer that throws, a missing shared asset or a camera
        # mode without a graph fails here rather than in somebody's export.
        placeable = [
            spec for spec in TEMPLATE_PACKS.values() if spec["id"] != "captions"
        ]
        sample_text = {
            "text": "frame accurate", "term": "latency", "meaning": "the wait before a response",
            "label": "throughput", "title": "Editoro", "value": "1280", "caption": "test card",
            "items": "first item\nsecond item\nthird item", "left": "Before", "right": "After",
            "left_note": "one", "right_note": "two", "author": "Editoro",
            "code": "def render(frame):\n    return frame", "name": "Editoro",
            "handle": "@editoro", "subtitle": "a section", "kicker": "watch this",
            "top": "top text", "bottom": "bottom text", "number": "01", "kind": "SOURCE",
            "badge": "VS",
        }
        instances: list[Instance] = []
        slot = 0.30
        for index, spec in enumerate(placeable):
            fields: dict[str, Any] = {}
            for field in spec["fields"]:
                kind = field["type"]
                if "default" in field:
                    fields[field["name"]] = copy.deepcopy(field["default"])
                elif kind in {"text", "textarea"}:
                    fields[field["name"]] = sample_text.get(field["name"], "editoro")
                elif kind == "asset:image":
                    fields[field["name"]] = asset.name
                elif kind == "asset:video":
                    fields[field["name"]] = inserted_video.name
                elif kind == "strokes":
                    fields[field["name"]] = [{"x1": .18, "y": .42, "x2": .82},
                                             {"x1": .22, "y": .55, "x2": .70}]
                elif kind in {"rect", "rect2"}:
                    fields[field["name"]] = {"x": .22, "y": .20, "w": .50, "h": .55}
                elif kind == "number":
                    fields[field["name"]] = float(field.get("default", field.get("min", 1)))
                elif kind == "select":
                    fields[field["name"]] = field["options"][0]
                elif kind == "boolean":
                    fields[field["name"]] = True
                elif kind == "color":
                    fields[field["name"]] = "#FFD166"
                if kind in {"text", "textarea"} and not fields.get(field["name"]):
                    fields[field["name"]] = sample_text.get(field["name"], "editoro")
            variant = spec["_variants"]["horizontal"]
            instances.append(Instance(
                id=f"e2e-{spec['id']}",
                template=spec["id"],
                # Cameras are put on their own low tracks and never overlap each
                # other; two camera moves at once is not a supported edit.
                track=1 + (index % 6),
                start=round(0.2 + index * slot, 3),
                duration=slot * 0.8,
                x=float(variant["x"]), y=float(variant["y"]),
                scale=float(variant["scale"]) * 0.7,
                fields=fields,
                # A pack that ships the frost switched on gets it here too, so
                # the export this test runs actually builds the multi-plane
                # graph - crop, alphamerge and all - rather than the single
                # composite every other block takes.
                backdrop=bool(spec.get("backdrop")),
            ))
        cameras = [item for item in instances if item.template in camera_packs()]
        for order, item in enumerate(cameras):
            item.track = 1
            item.start = round(0.2 + (len(placeable) + order) * slot, 3)
        state = ProjectState(
            name=name,
            source=source.name,
            source_info=info,
            cuts=[
                Cut(src_in=0, src_out=1.4),
                Cut(src_in=1.8, src_out=info.duration),
            ],
            instances=instances,
            captions=[
                CaptionBlock(
                    id="caption", start=.3, end=1.2, text="Editoro export test",
                    words=[
                        CaptionWord(w="Editoro", s=.3, e=.6),
                        CaptionWord(w="export", s=.6, e=.9),
                        CaptionWord(w="test", s=.9, e=1.2),
                    ],
                ),
                CaptionBlock(id="pinned", start=1.4, end=2.2, text="pinned caption",
                             follow_global=False, x=.30, y=.24),
            ],
        )
        assert len(state.instances) == len(placeable), "an instance was dropped by validation"
        save_state(state)
        expected_duration = sum(cut.src_out - cut.src_in for cut in state.cuts)
        for mode, resolution, expected_width in (("hq", "720", 1280), ("lossless", "source", 640)):
            before = set((folder / "exports").glob("*.mp4"))
            asyncio.run(export(name, mode, resolution))
            created = set((folder / "exports").glob("*.mp4")) - before
            assert len(created) == 1, (mode, (folder / "exports" / "export.log").read_text(errors="replace"))
            output = created.pop()
            verified = probe(output)
            assert verified.width == expected_width, (mode, verified)
            assert verified.audio_codec == "aac", (mode, verified)
            assert abs(verified.duration - expected_duration) <= 1 / info.fps + .01, (mode, verified.duration)
            export_log = (folder / "exports" / "export.log").read_text(errors="replace")
            assert "EXPORT FAILED" not in export_log
            assert "image2pipe" in export_log and "f%06d.png" not in export_log
            assert "Template renderer error" not in export_log
            assert "mpegts" in export_log, "spans must be joined through MPEG-TS"
            # The timeline carries a backdrop-blur block, so at least one span
            # must have taken the multi-plane route: the strip cropped back into
            # planes, the blur applied, and the mask's alpha fading it in. If
            # this stops appearing the frost has silently become a no-op.
            assert "crop=" in export_log and "gblur=sigma=" in export_log,                 "the backdrop plane never ran"
            assert "alphaextract" in export_log, "the blur was not masked by its plane"
        # A still has to come back from the same renderer, or the MCP server and
        # any agent looking at its own work are running blind.
        still = asyncio.run(render_still(name, 0.5, width=320))
        assert still[:8] == b"\x89PNG\r\n\x1a\n" and len(still) > 2000
        # ---- a block behind the speaker -------------------------------------
        # This is the one plane the run above cannot reach: it needs a subject
        # matte, and a synthetic project has never had the Look run over it. So
        # build a matte - a white column on black, which is what a silhouette of
        # someone standing in the middle of frame amounts to - and export again.
        # Without this the depth path would ship having only ever been checked
        # as a string.
        look_dir(name).mkdir(parents=True, exist_ok=True)
        run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"color=c=black:s=640x360:r={info.fps:.6f}:d={info.duration + 1:.3f}",
            "-vf", "drawbox=x=iw/3:y=0:w=iw/3:h=ih:color=white:t=fill,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-an", str(matte_path(name)),
        ], check=True)
        behind_state = load_state(name)
        behind_state.look.matte_revision = behind_state.source_revision
        assert matte_ready(behind_state), "the synthetic matte was not accepted"
        target = next(item for item in behind_state.instances
                      if item.template == "keyword")
        target.depth = "behind"
        target.start, target.duration = 0.2, 1.0
        save_state(behind_state)
        probe_seg = {"src_in": 0.2, "src_out": 1.2, "tl": 0.2, "camera": None}
        assert "behind" in overlay_slots(behind_state, probe_seg), (
            overlay_slots(behind_state, probe_seg))
        before = set((folder / "exports").glob("*.mp4"))
        asyncio.run(export(name, "hq", "720"))
        created = set((folder / "exports").glob("*.mp4")) - before
        behind_log = (folder / "exports" / "export.log").read_text(errors="replace")
        assert len(created) == 1, behind_log[-2000:]
        assert "EXPORT FAILED" not in behind_log, behind_log[-2000:]
        # The labels the depth composite uses, so this fails loudly if the plane
        # is dropped rather than quietly exporting the block in front.
        assert "[dpsubject]" in behind_log and "[dpcam]" in behind_log, (
            "the behind-the-speaker plane never ran")
        assert probe(created.pop()).width == 1280

        print(f"synthetic HQ + smart-lossless export tests OK "
              f"({len(state.instances)} template packs, 2 modes, still frame, "
              f"backdrop + behind planes)")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ----------------------------------------------------------- [11] Entrypoint
def local_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def open_browser_when_ready() -> None:
    """Open the editor only after this server answers its health endpoint."""
    from urllib.request import urlopen
    url = f"http://127.0.0.1:{PORT}"
    for _ in range(80):
        try:
            with urlopen(f"{url}/api/health", timeout=0.25) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.25)


if __name__ == "__main__":
    if "--test-e2e" in sys.argv:
        run_self_tests(); run_export_e2e_tests(); sys.exit(0)
    if "--test" in sys.argv:
        run_self_tests(); sys.exit(0)
    scan_templates()
    print("\n" + "=" * 52)
    print(f"  EDITORO  -> http://{local_ip()}:{PORT}   (LAN)")
    print(f"             http://127.0.0.1:{PORT}       (this machine)")
    print("=" * 52 + "\n")
    if "--no-browser" not in sys.argv:
        threading.Thread(target=open_browser_when_ready, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
