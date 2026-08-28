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
import asyncio, base64, json, math, mimetypes, os, re, shutil, struct, subprocess, sys, tempfile, threading, time, uuid, webbrowser
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


def run(cmd: list[str], log_file: Optional[Path] = None, check: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess, optionally appending full command + output to a log."""
    p = subprocess.run(cmd, capture_output=True, text=True)
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


class Cut(BaseModel):
    """Kept segment of the source video, in source-time coordinates, ordered."""
    src_in: float
    src_out: float

    @model_validator(mode="after")
    def ordered(self):
        if self.src_in < 0 or self.src_out <= self.src_in:
            raise ValueError("cut range is invalid")
        return self


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
            for asset in spec.get("assets", []):
                if not (d / asset).is_file():
                    raise ValueError(f"missing asset: {asset}")
            spec["_has_render"] = (d / "render.js").exists()
            if not spec["_has_render"]:
                raise ValueError("render.js is required; the engine has no built-in renderers")
            TEMPLATE_PACKS[spec["id"]] = spec
            verb = spec.get("directive_verb")
            if verb:
                VERB_TO_TEMPLATE[verb] = (spec["id"], bool(spec.get("directive_review")))
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


def resolve_sfx_events(item: Instance, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve one template instance into deterministic preview/export events."""
    events: list[dict[str, Any]] = []
    for sound in spec.get("sfx", []):
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
                        "-f", "s16le", "-"], capture_output=True)
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
VERB    = Arabic command verb → template category:
          نص → keyword · صورة → image-pop · ب-رول → video-clip · سكرين → screen
          زوم → punch-in · ميم → image-pop (+review ⚠️) · مولّد → placeholder
Lines that cannot be parsed are collected into a review list — never dropped.
For نص, the text field = the words inside the quote (verbatim spoken words).
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
    VERB_RE = re.compile("(" + "|".join(map(re.escape, verbs)) + ")")
    out, review = [], []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-•*·").strip()
        if not line:
            continue
        vm = VERB_RE.search(line)
        tm = TS_ANY.search(line)
        qm = QUOTE.search(line)
        t = None
        if tm:
            t = _ts_to_sec(tm.group(1), tm.group(2), tm.group(3))
        elif qm:
            t = find_quote_time(qm.group(1), st.captions)
        if vm is None or t is None:
            review.append(raw); continue
        tid, flag = VERB_TO_TEMPLATE[vm.group(1)]
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
        "nvenc": has_nvenc(),
        "whisper": importlib.util.find_spec("faster_whisper") is not None,
        "whisper_models": str(WHISPER_MODELS_DIR),
        "cuda_gpu": bool(shutil.which("nvidia-smi")),
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
    candidates = [
        Path(sys.prefix) / "Lib" / "site-packages" / "nvidia" / "cudnn" / "bin",
        Path(sys.prefix) / "Lib" / "site-packages" / "nvidia" / "cublas" / "bin",
    ]
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


# ------------------------------------------- [10] Export — smart rendering
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
            dirty = any(x <= mid < y for x, y in spans)
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


def camera_filter_graph(st: ProjectState, seg: dict, fps: float) -> str:
    """Build the source-video transform for one segment."""
    camera_id = seg.get("camera")
    camera = next((item for item in st.instances if item.id == camera_id), None)
    if camera is None:
        midpoint = seg["tl"] + (seg["src_out"] - seg["src_in"]) / 2
        camera = camera_effect_at(st, midpoint)
    if camera is None:
        return "[0:v]setpts=PTS-STARTPTS[base]"

    plan = camera_plan(st, camera, fps)
    global_time = f"({seg['tl']:.9f}+on/{fps:.9f})"
    zoom, cx, cy = camera_expressions(plan, global_time)
    width, height = st.source_info.width, st.source_info.height
    peak = max(plan["first"]["zoom"], plan["second"]["zoom"])
    # zoompan samples the frame it is handed. Handing it a 2x lanczos upscale
    # first is what separates a punch-in that looks intentional from one that
    # looks like a soft crop, and it also halves zoompan's integer-pixel jitter.
    supersample = ",scale=iw*2:ih*2:flags=lanczos" if peak > 1.12 else ""
    return (
        f"[0:v]setpts=PTS-STARTPTS{supersample},"
        f"zoompan=z='{zoom}':"
        f"x='clip({cx}*iw-iw/(2*zoom),0,iw-iw/zoom)':"
        f"y='clip({cy}*ih-ih/(2*zoom),0,ih-ih/zoom)':"
        f"d=1:s={width}x{height}:fps={fps:.9f}"
        "[base]"
    )


def punch_filter_graph(st: ProjectState, seg: dict, fps: float) -> str:
    """Backward-compatible name for older tests and integrations."""
    return camera_filter_graph(st, seg, fps)


class OverlayRenderSession:
    """One shared browser page per export, regardless of dirty segment count."""

    def __init__(self, name: str, state: ProjectState):
        self.name = name
        self.state = state
        self.playwright = None
        self.browser = None
        self.page = None
        self.errors: list[str] = []

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        executable = renderer_executable()
        if not executable:
            raise RuntimeError(
                "The export renderer is not installed. Close Editoro, run launch.cmd once, "
                "and let setup finish."
            )
        self.playwright = await async_playwright().start()
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
        self.page = await self.browser.new_page(viewport={
            "width": self.state.source_info.width,
            "height": self.state.source_info.height,
        })
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on(
            "console",
            lambda message: self.errors.append(message.text)
            if message.type == "error" else None,
        )
        snapshot = json.dumps(self.state.model_dump(), ensure_ascii=True)
        await self.page.add_init_script(
            f"window.__EDITORO_EXPORT_STATE = {snapshot};"
        )
        await self.page.goto(
            f"http://127.0.0.1:{PORT}/?export=1&project={self.name}"
        )
        try:
            await self.page.wait_for_function(
                "window.__exportReady === true || window.__exportError",
                timeout=60000,
            )
        except Exception as exc:
            detail = " | ".join(self.errors[-5:]) or str(exc)
            raise RuntimeError(f"Export renderer did not become ready: {detail}") from exc
        setup_error = await self.page.evaluate("window.__exportError || null")
        if setup_error:
            raise RuntimeError(f"Export renderer failed to start: {setup_error}")

    async def frame_batches(
        self,
        seg: dict,
        cancel: asyncio.Event,
        batch_size: int = 4,
    ):
        if not self.page:
            raise RuntimeError("overlay renderer is not started")
        fps = self.state.source_info.fps or 30
        count = max(1, int(math.ceil((seg["src_out"] - seg["src_in"]) * fps)))
        for first in range(0, count, batch_size):
            if cancel.is_set():
                raise asyncio.CancelledError()
            times = [
                seg["tl"] + frame / fps
                for frame in range(first, min(count, first + batch_size))
            ]
            data_urls = await self.page.evaluate(
                "times => window.__renderFrames(times)",
                times,
            )
            frames = [
                __import__("base64").b64decode(data.split(",", 1)[1])
                for data in data_urls
            ]
            yield first, count, frames
        if self.errors:
            raise RuntimeError(
                "Template renderer error: " + " | ".join(self.errors[-5:])
            )

    async def close(self) -> None:
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
    source_filter = camera_filter_graph(st, seg, fps)
    scale_filter = ""
    if resolution != "source":
        target_h = int(resolution)
        target_w = int(round(target_h * st.source_info.width / st.source_info.height / 2) * 2)
        scale_filter = f",scale={target_w}:{target_h}:flags=lanczos"
    # The base is padded with a cloned final frame and the output is bounded by
    # an exact frame count. Previously this relied on overlay's `shortest`, so a
    # single rounding disagreement between the decoder and the frame generator
    # shortened the span and every later cut drifted against the audio.
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{seg['src_in']:.6f}", "-t", f"{duration + 0.5:.6f}", "-i", str(src),
        "-thread_queue_size", "64", "-f", "image2pipe",
        "-framerate", f"{fps:.6f}", "-vcodec", "png", "-i", "pipe:0",
        "-filter_complex",
        f"{source_filter};[base]tpad=stop=-1:stop_mode=clone[padded];"
        f"[1:v]setpts=PTS-STARTPTS[ov];"
        f"[padded][ov]overlay=0:0:format=auto:eof_action=pass:shortest=0"
        f"{scale_filter},fps={fps:.6f}[v]",
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
    )
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        async for first, count, frames in renderer.frame_batches(seg, cancel):
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
            graph = camera_filter_graph(st, segment, fps)
            command = [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{source_time:.6f}", "-i", str(folder / st.source),
                "-filter_complex", graph, "-map", "[base]", "-frames:v", "1", str(base),
            ]
            result = run(command)
            if result.returncode or not base.is_file():
                raise HTTPException(500, (result.stderr or "could not read that frame")[-400:])

            renderer = OverlayRenderSession(name, st)
            await renderer.start()
            try:
                data_url = await renderer.page.evaluate(
                    "t => window.__renderFrame(t)", timeline_time)
            finally:
                await renderer.close()
            overlay = work / "overlay.png"
            overlay.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))

            out = work / "frame.png"
            scale = f",scale={int(width)}:-2:flags=lanczos" if width else ""
            result = run([
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(base), "-i", str(overlay),
                "-filter_complex", f"[0:v][1:v]overlay=0:0:format=auto{scale}[v]",
                "-map", "[v]", "-frames:v", "1", str(out),
            ])
            if result.returncode or not out.is_file():
                raise HTTPException(500, (result.stderr or "could not compose that frame")[-400:])
            return out.read_bytes()


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
        f"mode={mode} resolution={resolution} orientation={st.orientation}\n",
        encoding="utf-8",
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = exp / f"{name}-{stamp}.mp4"
    src = d / st.source
    fps = st.source_info.fps or 30
    nvenc = has_nvenc()
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
        copy_compatible = st.source_info.codec in {"h264", "avc1", "hevc", "h265"}
        annexb = bitstream_filter(st)

        async def encode_plain(seg_in: float, seg_out: float, target: Path) -> None:
            """Re-encode one untouched span, matched to the source parameters."""
            span = seg_out - seg_in
            count = max(1, int(round(span * fps)))
            command = [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{seg_in:.6f}", "-t", f"{span + 0.5:.6f}", "-i", str(src),
            ]
            filters = [f"fps={fps:.6f}"]
            if resolution != "source":
                target_h = int(resolution)
                target_w = int(round(target_h * st.source_info.width
                                     / max(1, st.source_info.height) / 2) * 2)
                filters.append(f"scale={target_w}:{target_h}:flags=lanczos")
            filters.append("tpad=stop=-1:stop_mode=clone")
            command += [
                "-vf", ",".join(filters), *vcodec, "-frames:v", str(count),
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
                    renderer = OverlayRenderSession(name, st)
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


def run_self_tests() -> None:
    """Fast deterministic checks used by launch-independent verification."""
    _test_parser()
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

    sample = ProjectState(
        name="selftest", source_info=MediaInfo(duration=10),
        cuts=[Cut(src_in=0, src_out=10)],
        instances=[Instance(id="x", template="keyword", start=2, duration=2)],
    )
    pieces = timeline_segments(sample)
    assert [(p["src_in"], p["src_out"], p["dirty"]) for p in pieces] == [
        (0.0, 2.0, False), (2.0, 4.0, True), (4.0, 10.0, False)
    ]
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
        # A still has to come back from the same renderer, or the MCP server and
        # any agent looking at its own work are running blind.
        still = asyncio.run(render_still(name, 0.5, width=320))
        assert still[:8] == b"\x89PNG\r\n\x1a\n" and len(still) > 2000
        print(f"synthetic HQ + smart-lossless export tests OK "
              f"({len(state.instances)} template packs, 2 modes, still frame)")
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
