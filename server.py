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
import asyncio, json, math, mimetypes, os, re, shutil, struct, subprocess, sys, threading, time, uuid, webbrowser
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
APP_VERSION = "1.3.0"
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
    fields: dict[str, Any] = Field(default_factory=dict)  # text, asset, strokes, rect, volume...
    missing: bool = False             # asset not yet filled
    review: bool = False              # ⚠️ (meme directives)


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

    @model_validator(mode="after")
    def ordered(self):
        if self.start < 0 or self.end <= self.start:
            raise ValueError("caption timestamps are invalid")
        return self


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
    directives_review: list[str] = Field(default_factory=list)  # unparseable lines
    ripple: bool = True

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not SAFE_PROJECT.fullmatch(value):
            raise ValueError("project names may contain letters, numbers, underscores, and hyphens")
        return value

    @model_validator(mode="after")
    def coherent(self):
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


def scan_templates() -> None:
    TEMPLATE_PACKS.clear()
    VERB_TO_TEMPLATE.clear()
    TEMPLATE_ERRORS.clear()
    colors: dict[str, str] = {}
    for d in sorted(TEMPLATES_DIR.iterdir()):
        if not d.is_dir():
            continue
        tj = d / "template.json"
        if not tj.exists():
            continue
        try:
            spec = json.loads(tj.read_text(encoding="utf-8"))
            required = {"id", "display_name", "category_color", "fields", "zones", "animation", "default_duration"}
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
                if field.get("type") not in {"text", "asset:image", "asset:video", "strokes", "rect", "none", "number"}:
                    raise ValueError(f"unsupported field type: {field.get('type')}")
                name = field.get("name")
                if not isinstance(name, str) or not name or name in names:
                    raise ValueError("field names must be non-empty and unique")
                names.add(name)
            for orientation in ("horizontal", "vertical"):
                zone = spec["zones"].get(orientation)
                if not isinstance(zone, dict):
                    raise ValueError(f"missing {orientation} placement zone")
                for key in ("x", "y", "scale"):
                    if not isinstance(zone.get(key), (int, float)):
                        raise ValueError(f"{orientation}.{key} must be numeric")
            duration = float(spec["default_duration"])
            if duration <= 0 and spec["id"] != "captions":
                raise ValueError("default_duration must be positive")
            for sfx in spec.get("sfx", []):
                if not (d / sfx["file"]).is_file():
                    raise ValueError(f"missing SFX: {sfx['file']}")
            for asset in spec.get("assets", []):
                if not (d / asset).is_file():
                    raise ValueError(f"missing asset: {asset}")
            spec["_dir"] = d.name
            spec["_has_render"] = (d / "render.js").exists()
            TEMPLATE_PACKS[spec["id"]] = spec
            v = spec.get("directive_verb")
            if v:
                VERB_TO_TEMPLATE[v] = (spec["id"], False)
        except Exception as e:
            TEMPLATE_ERRORS[d.name] = str(e)
            print(f"[templates] skipping {d.name}: {e}")
    # Grammar-level verbs not owned by a pack:
    if "image-pop" in TEMPLATE_PACKS:
        VERB_TO_TEMPLATE["ميم"] = ("image-pop", True)     # meme → image-pop + review flag
    VERB_TO_TEMPLATE["مولّد"] = ("__placeholder__", True)  # generated → generic placeholder
    VERB_TO_TEMPLATE["مولد"] = ("__placeholder__", True)


# --------------------------------------- [4] Template foley event resolution
def resolve_sfx_events(item: Instance, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve one template instance into deterministic preview/export events."""
    events: list[dict[str, Any]] = []
    for sound in spec.get("sfx", []):
        repeated = item.fields.get(sound.get("repeat_field", ""))
        count = max(1, len(repeated) if isinstance(repeated, list) else 1)
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


def group_caption_words(words: list[CaptionWord]) -> list[CaptionBlock]:
    """Group word timestamps into readable, editable two-line caption blocks."""
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

    for word in ordered:
        candidate = " ".join([*(item.w for item in buffer), word.w])
        previous_ended = bool(buffer and re.search(r"[.!?؟…]$", buffer[-1].w))
        gap = word.s - buffer[-1].e if buffer else 0
        if buffer and (
            len(buffer) >= 7
            or len(candidate) > 44
            or word.e - buffer[0].s > 3.5
            or gap > 0.65
            or previous_ended
        ):
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
    return {"packs": list(TEMPLATE_PACKS.values())}


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
    require_project(name)
    payload = await req.json()
    payload["name"] = name
    st = ProjectState.model_validate(payload)
    save_state(st)
    return {"ok": True}


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


CAMERA_TEMPLATES = {"punch-in", "zoom-out"}


def camera_effect_at(st: ProjectState, timeline_time: float) -> Optional[Instance]:
    active = [
        item for item in st.instances
        if item.template in CAMERA_TEMPLATES
        and isinstance(item.fields.get("rect"), dict)
        and item.start <= timeline_time < item.start + item.duration
    ]
    return min(active, key=lambda item: (item.track, item.start, item.id), default=None)


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
    """Choose a fast, concat-compatible high-quality encoder."""
    hevc = st.source_info.codec in {"hevc", "h265"}
    if nvenc:
        codec = "hevc_nvenc" if hevc else "h264_nvenc"
        return [
            "-c:v", codec, "-preset", "p4", "-tune", "hq",
            "-rc", "constqp", "-qp", "16", "-spatial_aq", "1", "-aq-strength", "8",
            "-pix_fmt", "yuv420p",
        ]
    codec = "libx265" if hevc else "libx264"
    return [
        "-c:v", codec, "-preset", "veryfast", "-crf", "16",
        "-pix_fmt", "yuv420p",
    ]


def camera_filter_graph(st: ProjectState, seg: dict, fps: float) -> str:
    """Build the source-video transform shared by zoom-in and zoom-out."""
    camera_id = seg.get("camera")
    camera = next((item for item in st.instances if item.id == camera_id), None)
    if camera is None:
        midpoint = seg["tl"] + (seg["src_out"] - seg["src_in"]) / 2
        camera = camera_effect_at(st, midpoint)
    if camera is None:
        return "[0:v]setpts=PTS-STARTPTS[base]"

    rect = camera.fields["rect"]
    width = max(0.05, min(1.0, float(rect.get("w", 1.0))))
    height = max(0.05, min(1.0, float(rect.get("h", 1.0))))
    x = max(0.0, min(1.0 - width, float(rect.get("x", 0.0))))
    y = max(0.0, min(1.0 - height, float(rect.get("y", 0.0))))
    center_x, center_y = x + width / 2, y + height / 2
    target = 1.0 / max(width, height)
    animation = TEMPLATE_PACKS.get(camera.template, {}).get("animation", {})
    entrance = max(1 / fps, float(animation.get("entrance_ms", 450)) / 1000)
    exit_duration = max(1 / fps, float(animation.get("exit_ms", 400)) / 1000)
    start, end = camera.start, camera.start + camera.duration
    global_time = f"({seg['tl']:.9f}+on/{fps:.9f})"
    in_k = f"clip(({global_time}-{start:.9f})/{entrance:.9f},0,1)"
    out_k = f"clip(({end:.9f}-{global_time})/{exit_duration:.9f},0,1)"
    ease_in = f"(1-pow(1-{in_k},3))"
    ease_out = f"(1-pow(1-{out_k},3))"
    if camera.template == "zoom-out":
        amount = f"if(lt({global_time},{start + entrance:.9f}),1-{ease_in},0)"
    else:
        amount = (
            f"if(lt({global_time},{start + entrance:.9f}),{ease_in},"
            f"if(gt({global_time},{end - exit_duration:.9f}),{ease_out},1))"
        )
    zoom = f"(1+{target - 1:.9f}*{amount})"
    return (
        "[0:v]setpts=PTS-STARTPTS,"
        f"zoompan=z='{zoom}':"
        f"x='clip({center_x:.9f}*iw-iw/(2*zoom),0,iw-iw/zoom)':"
        f"y='clip({center_y:.9f}*ih-ih/(2*zoom),0,ih-ih/zoom)':"
        f"d=1:s={st.source_info.width}x{st.source_info.height}:fps={fps:.9f}"
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
                "window.__exportReady === true",
                timeout=30000,
            )
        except Exception as exc:
            detail = " | ".join(self.errors[-5:]) or str(exc)
            raise RuntimeError(f"Export renderer did not become ready: {detail}") from exc

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
    source_filter = camera_filter_graph(st, seg, fps)
    scale_filter = ""
    if resolution != "source":
        target_h = int(resolution)
        target_w = int(round(target_h * st.source_info.width / st.source_info.height / 2) * 2)
        scale_filter = f",scale={target_w}:{target_h}:flags=lanczos"
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(seg["src_in"]), "-t", str(duration), "-i", str(src),
        "-thread_queue_size", "32", "-f", "image2pipe",
        "-framerate", str(fps), "-vcodec", "png", "-i", "pipe:0",
        "-filter_complex",
        f"{source_filter};[1:v]setpts=PTS-STARTPTS[ov];"
        f"[base][ov]overlay=0:0:format=auto:shortest=1{scale_filter}[v]",
        "-map", "[v]", *vcodec, "-r", str(fps), "-an",
        "-movflags", "+faststart", str(part),
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
    log.write_text(f"Editoro {APP_VERSION} export started {datetime.now().isoformat()}\n", encoding="utf-8")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = exp / f"{name}-{stamp}.mp4"
    src = d / st.source
    fps = st.source_info.fps or 30
    nvenc = has_nvenc()
    vcodec = video_encoder(st, nvenc)
    if not nvenc:
        await progress(name, note="NVENC unavailable — using the fast high-quality CPU encoder")
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
        kfs = keyframes(src, log)
        parts: list[Path] = []
        work.mkdir()
        copy_compatible = st.source_info.codec in {"h264", "avc1", "hevc", "h265"}
        for idx, seg in enumerate(segs):
            if cancel.is_set():
                raise asyncio.CancelledError()
            await progress(name, seg=idx + 1, total=len(segs),
                           status="copy" if not seg["dirty"] and mode == "lossless" else "encode")
            part = work / f"p{idx:03d}.mp4"
            duration = seg["src_out"] - seg["src_in"]
            if not seg["dirty"] and mode == "lossless" and copy_compatible:
                # smart cut: snap start forward to a keyframe; re-encode only the
                # pre-keyframe slice if the requested cut isn't keyframe-aligned.
                next_keyframe = min(
                    (k for k in kfs if k >= seg["src_in"] - 1e-3),
                    default=None,
                )
                if next_keyframe is None or next_keyframe >= seg["src_out"] - 1 / fps:
                    # A copied span cannot begin accurately without a keyframe.
                    # Re-encode it once instead of pulling earlier frames in.
                    await run_cancelable([
                        FFMPEG, "-y", "-ss", str(seg["src_in"]),
                        "-t", str(seg["src_out"] - seg["src_in"]), "-i", str(src),
                        *vcodec, "-an", "-movflags", "+faststart", str(part),
                    ], log, cancel, check=True)
                elif next_keyframe - seg["src_in"] > 1 / fps:
                    sl = work / f"p{idx:03d}a.mp4"
                    slice_end = min(next_keyframe, seg["src_out"])
                    await run_cancelable([
                        FFMPEG, "-y", "-ss", str(seg["src_in"]),
                        "-t", str(slice_end - seg["src_in"]), "-i", str(src),
                        *vcodec, "-an", "-movflags", "+faststart", str(sl),
                    ], log, cancel, check=True)
                    parts.append(sl)
                    if next_keyframe >= seg["src_out"]:
                        continue
                    seg = {**seg, "src_in": next_keyframe}
                    await run_cancelable([
                        FFMPEG, "-y", "-ss", str(seg["src_in"]),
                        "-t", str(seg["src_out"] - seg["src_in"]), "-i", str(src),
                        "-c:v", "copy", "-an",
                        "-avoid_negative_ts", "make_zero", str(part),
                    ], log, cancel, check=True)
                else:
                    await run_cancelable([
                        FFMPEG, "-y", "-ss", str(seg["src_in"]),
                        "-t", str(seg["src_out"] - seg["src_in"]), "-i", str(src),
                        "-c:v", "copy", "-an",
                        "-avoid_negative_ts", "make_zero", str(part),
                    ], log, cancel, check=True)
            elif not seg["dirty"]:
                command = [
                    FFMPEG, "-y", "-ss", str(seg["src_in"]), "-t", str(duration),
                    "-i", str(src),
                ]
                if resolution != "source":
                    target_h = int(resolution)
                    target_w = int(round(target_h * st.source_info.width / st.source_info.height / 2) * 2)
                    command += ["-vf", f"scale={target_w}:{target_h}:flags=lanczos"]
                command += [
                    *vcodec, "-r", str(fps),
                    "-an", "-movflags", "+faststart",
                    str(part),
                ]
                await run_cancelable(command, log, cancel, check=True)
            else:
                if renderer is None:
                    renderer = OverlayRenderSession(name, st)
                    await renderer.start()
                await encode_dirty_segment(
                    renderer, st, seg, src, part, vcodec, resolution, log, cancel,
                    name, idx + 1, len(segs),
                )
            parts.append(part)
        # concat
        lst = work / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        pre = work / "video.mp4"
        concat = await run_cancelable([
            FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c:v", "copy", "-an", "-movflags", "+faststart", str(pre),
        ], log, cancel)
        if concat.returncode:
            await run_cancelable([
                FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                *vcodec, "-an", "-movflags", "+faststart", str(pre),
            ], log, cancel, check=True)
        expected_video_duration = sum(
            segment["src_out"] - segment["src_in"] for segment in segs
        )
        joined = probe(pre)
        if abs(joined.duration - expected_video_duration) > max(.08, 2 / fps):
            raise RuntimeError(
                "Video segment timestamps did not join accurately "
                f"({joined.duration:.3f}s vs {expected_video_duration:.3f}s)"
            )
        # Build speech once from source cuts. Encoding audio in every tiny video
        # segment adds AAC priming at every join and causes duration drift.
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
                speech_filters.append(
                    f"[0:a]asplit={len(cuts)}{''.join(source_labels)}"
                )
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
                FFMPEG, "-y", "-i", str(src), "-filter_complex", ";".join(speech_filters),
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
                f = TEMPLATES_DIR / spec["_dir"] / event["file"]
                if f.exists():
                    inputs.append(str(f))
                    input_index = len(inputs) - 1
                    delay = int(event["time"] * 1000)
                    filters.append(
                        f"[{input_index}:a]adelay={delay}:all=1,"
                        f"volume={event['gain']}[s{input_index}]"
                    )
                    amix.append(f"[s{input_index}]")
            if i.template == "video-clip" and i.fields.get("asset") and not i.missing:
                af = d / "assets" / i.fields["asset"]
                try:
                    has_clip_audio = af.exists() and bool(probe(af).audio_codec)
                except ValueError:
                    has_clip_audio = False
                if has_clip_audio:
                    inputs.append(str(af))
                    input_index = len(inputs) - 1
                    delay = int(i.start * 1000)
                    filters.append(
                        f"[{input_index}:a]atrim=0:{i.duration},asetpts=PTS-STARTPTS,"
                        f"adelay={delay}:all=1,volume={i.fields.get('volume',0.8)}[s{input_index}]"
                    )
                    amix.append(f"[s{input_index}]")
        if len(amix) == 1:
            filters.append(f"{amix[0]}atrim=0:{total_duration},aresample=48000[a]")
        else:
            filters.append(
                f"{''.join(amix)}amix=inputs={len(amix)}:duration=first:normalize=0,"
                f"atrim=0:{total_duration},aresample=48000[a]"
            )
        fc = ";".join(filters)
        cmd = [FFMPEG, "-y"]
        for x in inputs:
            cmd += ["-i", x]
        cmd += [
            "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-shortest",
            "-movflags", "+faststart", str(partial),
        ]
        await run_cancelable(cmd, log, cancel, check=True)
        verified = probe(partial)
        if not verified.codec or verified.duration <= 0:
            raise RuntimeError("FFmpeg created an invalid output file")
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
    assert len(TEMPLATE_PACKS) == 9, sorted(TEMPLATE_PACKS)
    assert not TEMPLATE_ERRORS, TEMPLATE_ERRORS
    for spec in TEMPLATE_PACKS.values():
        folder = TEMPLATES_DIR / spec["_dir"]
        assert (folder / "context_template.md").is_file()
        assert (folder / "render.js").is_file(), f"{spec['id']} has no renderer"
        assert spec["_has_render"] is True
        for filename in spec.get("assets", []):
            assert (folder / filename).is_file(), (spec["id"], filename)
    assert not TEMPLATE_PACKS["punch-in"].get("sfx")
    assert not TEMPLATE_PACKS["zoom-out"].get("sfx")

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
            assert (await client.get("/api/health")).json()["templates"] == 9
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
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=3",
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
        state = ProjectState(
            name=name,
            source=source.name,
            source_info=info,
            cuts=[
                Cut(src_in=0, src_out=1.4),
                Cut(src_in=1.8, src_out=info.duration),
            ],
            instances=[
                Instance(
                    id="ambient", template="ambient", track=1, start=.1, duration=.4,
                ),
                Instance(
                    id="image", template="image-pop", track=2, start=.2, duration=.6,
                    x=.72, y=.35, scale=.7, fields={"asset": asset.name},
                ),
                Instance(
                    id="highlight", template="highlight", track=3, start=.4, duration=.8,
                    x=.32, y=.34, scale=.65,
                    fields={
                        "asset": asset.name,
                        "page": 1,
                        "strokes": [{"x1": .18, "y": .42, "x2": .82}],
                    },
                ),
                Instance(
                    id="screen", template="screen", track=4, start=.8, duration=.6,
                    x=.65, y=.64, scale=.55, fields={"asset": asset.name},
                ),
                Instance(
                    id="punch", template="punch-in", track=5, start=1, duration=.8,
                    fields={"rect": {"x": .25, "y": .25, "w": .5, "h": .5}},
                ),
                Instance(
                    id="zoom-out", template="zoom-out", track=6, start=2, duration=.5,
                    fields={"rect": {"x": .2, "y": .2, "w": .6, "h": .6}},
                ),
                Instance(
                    id="video", template="video-clip", track=2, start=1.4, duration=.6,
                    x=.28, y=.66, scale=.55,
                    fields={"asset": inserted_video.name, "volume": .25},
                ),
                Instance(
                    id="keyword", template="keyword", track=3, start=2.1, duration=.5,
                    x=.5, y=.15, scale=.8, fields={"text": "frame accurate"},
                ),
            ],
            captions=[
                CaptionBlock(id="caption", start=.3, end=1.2, text="Editoro export test"),
            ],
        )
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
        print("synthetic HQ + smart-lossless export tests OK")
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
