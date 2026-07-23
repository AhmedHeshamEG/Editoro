# ============================================================================
#  EDITORO — server.py
#  Single-file backend: FastAPI + uvicorn + ffmpeg/ffprobe subprocesses.
#  Sections:
#    [1] Imports & config
#    [2] Pydantic models (project state)
#    [3] Template pack scanning
#    [4] First-run placeholder SFX generation (pure-python wav synthesis)
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
import asyncio, json, math, os, re, shutil, struct, subprocess, sys, threading, time, uuid, wave, webbrowser
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

PORT = 8765
APP_VERSION = "1.0.0"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
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
        self.cuts.sort(key=lambda c: c.src_in)
        if self.source_info.duration:
            for cut in self.cuts:
                if cut.src_out > self.source_info.duration + 0.05:
                    raise ValueError("cut exceeds source duration")
        self.instances.sort(key=lambda item: (item.start, item.track, item.id))
        self.captions.sort(key=lambda item: (item.start, item.id))
        return self


def project_dir(name: str) -> Path:
    if not SAFE_PROJECT.fullmatch(name):
        raise ValueError("invalid project name")
    p = (PROJECTS_DIR / name).resolve()
    if PROJECTS_DIR not in p.parents:
        raise ValueError("bad project name")
    return p


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


def save_state(st: ProjectState) -> None:
    d = project_dir(st.name)
    d.mkdir(parents=True, exist_ok=True)
    target = d / "project.json"
    temporary = d / ".project.json.tmp"
    temporary.write_text(st.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, target)


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


def scan_templates() -> None:
    TEMPLATE_PACKS.clear()
    VERB_TO_TEMPLATE.clear()
    for d in sorted(TEMPLATES_DIR.iterdir()):
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
            for field in spec["fields"]:
                if field.get("type") not in {"text", "asset:image", "asset:video", "strokes", "rect", "none", "number"}:
                    raise ValueError(f"unsupported field type: {field.get('type')}")
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
            print(f"[templates] skipping {d.name}: {e}")
    # Grammar-level verbs not owned by a pack:
    if "image-pop" in TEMPLATE_PACKS:
        VERB_TO_TEMPLATE["ميم"] = ("image-pop", True)     # meme → image-pop + review flag
    VERB_TO_TEMPLATE["مولّد"] = ("__placeholder__", True)  # generated → generic placeholder
    VERB_TO_TEMPLATE["مولد"] = ("__placeholder__", True)


# ------------------- [4] First-run placeholder SFX (pure-python synthesis)
# Real packs ship recorded organic sounds. Until the user drops in his own
# wavs, we synthesize short organic-ish placeholders so the pipeline works
# end-to-end. Simplest robust option; regenerate = delete the wav.
def _write_wav(path: Path, samples: list[float], sr: int = 44100) -> None:
    with wave.open(str(path), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32000)) for s in samples))


def _noise_burst(dur: float, decay: float, lp: float = 0.3, sr: int = 44100) -> list[float]:
    import random
    rnd = random.Random(7)
    out, prev = [], 0.0
    n = int(dur * sr)
    for i in range(n):
        v = rnd.uniform(-1, 1) * math.exp(-i / (decay * sr))
        prev = prev + lp * (v - prev)   # one-pole low-pass = softer, papery
        out.append(prev)
    return out


def ensure_placeholder_sfx() -> None:
    wants = {  # (pack dir, filename, kind)
        ("keyword", "paper-pop.wav", 0.10), ("image-pop", "paper-slap.wav", 0.14),
        ("video-clip", "paper-slap.wav", 0.14), ("highlight", "marker-sweep.wav", 0.35),
        ("punch-in", "whoosh.wav", 0.22), ("screen", "settle.wav", 0.12),
    }
    for pack, fn, dur in wants:
        p = TEMPLATES_DIR / pack / fn
        if not p.exists() and p.parent.exists():
            _write_wav(p, _noise_burst(dur, dur * 0.5))


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
ensure_placeholder_sfx()
app = FastAPI(title="Editoro", version=APP_VERSION)


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
            ))
        for segment in segments:
            segment_words = segment.get("words", []) if isinstance(segment, dict) else []
            if segment_words:
                for raw in segment_words:
                    words.append(CaptionWord(
                        w=str(raw.get("word", raw.get("w", ""))).strip(),
                        s=float(raw.get("start", raw.get("s", 0))),
                        e=float(raw.get("end", raw.get("e", 0))),
                    ))
            elif isinstance(segment, dict) and segment.get("text"):
                start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
                text = str(segment["text"]).strip()
                if text and end > start:
                    captions.append(CaptionBlock(
                        id=f"c-{uuid.uuid4().hex[:10]}", start=start, end=end, text=text
                    ))
        words = [word for word in words if word.w and word.e > word.s]
        words.sort(key=lambda item: item.s)
        buffer: list[CaptionWord] = []
        for word in words:
            sentence_end = bool(buffer and re.search(r"[.!?]$", buffer[-1].w))
            if buffer and (len(buffer) >= 7 or word.e - buffer[0].s > 3.5 or sentence_end):
                captions.append(CaptionBlock(
                    id=f"c-{uuid.uuid4().hex[:10]}", start=buffer[0].s,
                    end=buffer[-1].e, text=" ".join(item.w for item in buffer), words=buffer
                ))
                buffer = []
            buffer.append(word)
        if buffer:
            captions.append(CaptionBlock(
                id=f"c-{uuid.uuid4().hex[:10]}", start=buffer[0].s,
                end=buffer[-1].e, text=" ".join(item.w for item in buffer), words=buffer
            ))
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
    return {
        **api_health(),
        "nvenc": has_nvenc(),
        "python": sys.version.split()[0],
        "root": str(ROOT),
        "projects": len([
            p for p in PROJECTS_DIR.iterdir()
            if not p.name.startswith(".deleted-") and (p / "project.json").is_file()
        ]),
        "template_ids": sorted(TEMPLATE_PACKS),
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
    d = project_dir(name)
    if not d.exists():
        raise HTTPException(404, "Project not found")
    trash = PROJECTS_DIR / f".deleted-{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    d.rename(trash)
    return {"ok": True, "recoverable_at": trash.name}


@app.get("/api/projects/{name}/state")
def api_state(name: str):
    return JSONResponse(load_state(name).model_dump())


@app.post("/api/projects/{name}/state")
async def api_save(name: str, req: Request):
    st = ProjectState.model_validate(await req.json())
    st.name = name
    save_state(st)
    return {"ok": True}


@app.post("/api/projects/{name}/source")
async def api_source(name: str, file: UploadFile):
    d = project_dir(name)
    if not d.exists():
        raise HTTPException(404, "Project not found")
    filename = safe_upload_name(file.filename, VIDEO_EXTENSIONS)
    dest = d / ("source" + Path(filename).suffix.lower())
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    st = load_state(name)
    old_source = d / st.source if st.source else None
    st.source = dest.name
    try:
        st.source_info = probe(dest)
    except ValueError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(415, str(exc)) from exc
    if not st.source_info.codec or not st.source_info.width or not st.source_info.duration:
        dest.unlink(missing_ok=True)
        raise HTTPException(415, "The selected file does not contain a readable video stream")
    st.orientation = "vertical" if st.source_info.height > st.source_info.width else "horizontal"
    st.cuts = [Cut(src_in=0, src_out=st.source_info.duration)]
    save_state(st)
    if old_source and old_source != dest:
        old_source.unlink(missing_ok=True)
    (d / "waveform.json").unlink(missing_ok=True)
    return st.model_dump()


@app.post("/api/projects/{name}/asset")
async def api_asset(name: str, file: UploadFile):
    d = project_dir(name) / "assets"; d.mkdir(exist_ok=True)
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
    d = project_dir(name) / "assets"
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
    st = load_state(name)
    try:
        st.captions = parse_transcript(data, filename)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"Transcript could not be imported: {exc}") from exc
    suffix = Path(filename).suffix.lower()
    for old in project_dir(name).glob("transcript.*"):
        old.unlink(missing_ok=True)
    (project_dir(name) / f"transcript{suffix}").write_text(data, encoding="utf-8")
    save_state(st)
    return st.model_dump()


@app.get("/api/projects/{name}/waveform")
def api_waveform(name: str):
    return {"peaks": waveform_peaks(name)}


@app.get("/api/projects/{name}/thumb")
def api_thumb(name: str, t: float = 0):
    try:
        return FileResponse(thumbnail(name, t))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/projects/{name}/pdf-page")
def api_pdf(name: str, asset: str, page: int = 0):
    try:
        return FileResponse(raster_pdf(name, asset, page))
    except (ValueError, IndexError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/projects/{name}/directives")
async def api_directives(name: str, req: Request):
    body = await req.json()
    text = body.get("text", "")
    st = load_state(name)
    if len(text) > 1_000_000:
        raise HTTPException(413, "Directive text is too large")
    (project_dir(name) / "directives.txt").write_text(text, encoding="utf-8")
    inst, review = parse_directives(text, st)
    # place on first free overlay track (simple greedy per instance)
    for i in inst:
        tr = 1
        while any(o.track == tr and o.start < i.start + i.duration and i.start < o.start + o.duration
                  for o in st.instances):
            tr += 1
        i.track = tr
        st.instances.append(i)
    st.directives_review = review
    save_state(st)
    return st.model_dump()


# ------------------------------- [8] Range-request media serving (iOS Safari)
@app.get("/media/{name}/{path:path}")
def media(name: str, path: str, request: Request):
    f = (project_dir(name) / path).resolve()
    if project_dir(name) not in f.parents or not f.is_file():
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
                save_state(st)
                await HUB.broadcast(msg, exclude=ws)            # sync other devices
            elif msg.get("type") == "export":
                asyncio.create_task(export(msg["project"], msg.get("mode", "lossless")))
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


# ------------------------------------------- [10] Export — smart rendering
def has_nvenc() -> bool:
    listed = run([FFMPEG, "-hide_banner", "-encoders"])
    if "h264_nvenc" not in listed.stdout:
        return False
    probe_encode = run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=black:s=64x64:r=1", "-frames:v", "1",
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
    for s in segs:
        length = s["src_out"] - s["src_in"]
        marks = sorted({0.0, length} | {max(0, min(length, a - s["tl"])) for a, b in spans}
                       | {max(0, min(length, b - s["tl"])) for a, b in spans})
        for a, b in zip(marks, marks[1:]):
            if b - a < 1 / 120: continue
            mid = s["tl"] + (a + b) / 2
            dirty = any(x <= mid < y for x, y in spans)
            out.append({"src_in": s["src_in"] + a, "src_out": s["src_in"] + b,
                        "tl": s["tl"] + a, "dirty": dirty})
    return out


def video_encoder(st: ProjectState, nvenc: bool) -> list[str]:
    """Choose a concat-compatible visually-lossless encoder."""
    hevc = st.source_info.codec in {"hevc", "h265"}
    if nvenc:
        codec = "hevc_nvenc" if hevc else "h264_nvenc"
        return [
            "-c:v", codec, "-preset", "p6", "-rc", "constqp", "-qp", "14",
            "-pix_fmt", "yuv420p",
        ]
    codec = "libx265" if hevc else "libx264"
    return [
        "-c:v", codec, "-preset", "slow", "-crf", "12",
        "-pix_fmt", "yuv420p",
    ]


async def render_overlays(name: str, st: ProjectState, seg: dict, outdir: Path, log) -> Path:
    """Headless Playwright drives the SAME overlay engine (index.html?export=1),
    rendering transparent PNGs frame-by-frame → pixel-identical to preview."""
    from playwright.async_api import async_playwright
    fps = st.source_info.fps or 30
    outdir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        b = await pw.chromium.launch(args=["--disable-gpu-sandbox"])
        pg = await b.new_page(viewport={"width": st.source_info.width, "height": st.source_info.height})
        await pg.goto(f"http://127.0.0.1:{PORT}/?export=1&project={name}")
        await pg.wait_for_function("window.__exportReady === true", timeout=30000)
        count = max(1, int(math.ceil((seg["src_out"] - seg["src_in"]) * fps)))
        try:
            for frame in range(count):
                t = seg["tl"] + frame / fps
                data = await pg.evaluate("t => window.__renderFrame(t)", t)
                (outdir / f"f{frame:06d}.png").write_bytes(
                    __import__("base64").b64decode(data.split(",", 1)[1]))
        finally:
            await b.close()
    return outdir


EXPORT_CANCEL: dict[str, asyncio.Event] = {}


@app.get("/api/projects/{name}/exports")
def api_exports(name: str):
    folder = project_dir(name) / "exports"
    files = []
    if folder.exists():
        for path in sorted(folder.glob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True):
            files.append({
                "name": path.name, "size": path.stat().st_size,
                "url": f"/media/{name}/exports/{path.name}",
                "modified": path.stat().st_mtime,
            })
    return {"files": files}


async def export(name: str, mode: str):
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
        await progress(name, note="NVENC unavailable — falling back to libx264 -crf 12")
    work = exp / f"work-{stamp}"
    try:
        if not st.source or not src.is_file():
            raise ValueError("Import source footage before exporting")
        if mode not in {"lossless", "hq"}:
            raise ValueError("Unknown export mode")
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
                kf = min((k for k in kfs if k >= seg["src_in"] - 1e-3), default=seg["src_in"])
                if kf - seg["src_in"] > 1 / fps:      # slice before next keyframe → tiny re-encode
                    sl = work / f"p{idx:03d}a.mp4"
                    slice_end = min(kf, seg["src_out"])
                    run([FFMPEG, "-y", "-ss", str(seg["src_in"]),
                         "-t", str(slice_end - seg["src_in"]), "-i", str(src),
                         *vcodec, "-c:a", "aac", "-b:a", "256k",
                         "-movflags", "+faststart", str(sl)], log, check=True)
                    parts.append(sl)
                    if kf >= seg["src_out"]:
                        continue
                    seg = {**seg, "src_in": kf}
                run([FFMPEG, "-y", "-ss", str(seg["src_in"]),
                     "-t", str(seg["src_out"] - seg["src_in"]), "-i", str(src),
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
                     "-avoid_negative_ts", "make_zero", str(part)], log, check=True)
            elif not seg["dirty"]:
                run([FFMPEG, "-y", "-ss", str(seg["src_in"]), "-t", str(duration),
                     "-i", str(src), *vcodec, "-r", str(fps),
                     "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart",
                     str(part)], log, check=True)
            else:
                ov = await render_overlays(name, st, seg, work / f"ov{idx:03d}", log)
                run([FFMPEG, "-y", "-ss", str(seg["src_in"]), "-t", str(duration), "-i", str(src),
                     "-framerate", str(fps), "-i", str(ov / "f%06d.png"),
                     "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto:shortest=1[v]",
                     "-map", "[v]", "-map", "0:a?", *vcodec,
                     "-r", str(fps), "-c:a", "aac", "-b:a", "256k",
                     "-movflags", "+faststart", str(part)], log, check=True)
            parts.append(part)
        # concat
        lst = work / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        pre = work / "video.mp4"
        concat = run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                      "-c", "copy", "-movflags", "+faststart", str(pre)], log)
        if concat.returncode:
            run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                 *vcodec, "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart",
                 str(pre)], log, check=True)
        # audio mix: concatenated speech + SFX events + video-clip audio
        total_duration = sum(segment["src_out"] - segment["src_in"] for segment in segs)
        inputs, filters = [str(pre)], []
        if st.source_info.audio_codec:
            amix = ["[0:a]"]
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{total_duration}[base]")
            amix = ["[base]"]
        n = 1
        for i in st.instances:
            spec = TEMPLATE_PACKS.get(i.template, {})
            for s in spec.get("sfx", []):
                f = TEMPLATES_DIR / spec["_dir"] / s["file"]
                if f.exists():
                    inputs.append(str(f))
                    delay = int((i.start + s.get("offset", 0)) * 1000)
                    filters.append(f"[{n}:a]adelay={delay}:all=1,volume={s.get('gain',0.8)}[s{n}]")
                    amix.append(f"[s{n}]"); n += 1
            if i.template == "video-clip" and i.fields.get("asset") and not i.missing:
                af = d / "assets" / i.fields["asset"]
                if af.exists():
                    inputs.append(str(af))
                    delay = int(i.start * 1000)
                    filters.append(
                        f"[{n}:a]atrim=0:{i.duration},asetpts=PTS-STARTPTS,"
                        f"adelay={delay}:all=1,volume={i.fields.get('volume',0.8)}[s{n}]"
                    )
                    amix.append(f"[s{n}]"); n += 1
        if n > 1 or not st.source_info.audio_codec:
            filters.append(
                f"{''.join(amix)}amix=inputs={len(amix)}:duration=first:normalize=0,"
                f"atrim=0:{total_duration}[a]"
            )
            fc = ";".join(filters)
            cmd = [FFMPEG, "-y"]
            for x in inputs: cmd += ["-i", x]
            cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
                    "-movflags", "+faststart", str(final)]
            run(cmd, log, check=True)
        else:
            shutil.copy2(pre, final)
        verified = probe(final)
        if not verified.codec or verified.duration <= 0:
            raise RuntimeError("FFmpeg created an invalid output file")
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
        EXPORT_CANCEL.pop(name, None)


def run_self_tests() -> None:
    """Fast deterministic checks used by launch-independent verification."""
    _test_parser()
    assert len(TEMPLATE_PACKS) == 8, sorted(TEMPLATE_PACKS)
    for spec in TEMPLATE_PACKS.values():
        folder = TEMPLATES_DIR / spec["_dir"]
        assert (folder / "context_template.md").is_file()
        for filename in spec.get("assets", []):
            assert (folder / filename).is_file(), (spec["id"], filename)

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

    from fastapi.testclient import TestClient
    test_name = "editoro-selftest"
    test_folder = project_dir(test_name)
    if test_folder.exists():
        shutil.rmtree(test_folder)
    try:
        client = TestClient(app)
        assert client.get("/api/health").json()["templates"] == 8
        assert client.post(f"/api/projects/{test_name}").status_code == 200
        media_file = test_folder / "assets" / "range.bin"
        media_file.write_bytes(b"0123456789")
        ranged = client.get(f"/media/{test_name}/assets/range.bin", headers={"Range": "bytes=2-5"})
        assert ranged.status_code == 206 and ranged.content == b"2345"
        invalid = client.get(f"/media/{test_name}/assets/range.bin", headers={"Range": "bytes=99-"})
        assert invalid.status_code == 416
        transcript = client.post(
            f"/api/projects/{test_name}/transcript",
            files={"file": ("captions.srt", srt.encode("utf-8"), "application/x-subrip")},
        )
        assert transcript.status_code == 200 and len(transcript.json()["captions"]) == 2
    finally:
        if test_folder.exists():
            shutil.rmtree(test_folder)
    print("backend/API tests OK")


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
    if "--test" in sys.argv:
        run_self_tests(); sys.exit(0)
    scan_templates()
    ensure_placeholder_sfx()
    print("\n" + "=" * 52)
    print(f"  EDITORO  -> http://{local_ip()}:{PORT}   (LAN)")
    print(f"             http://127.0.0.1:{PORT}       (this machine)")
    print("=" * 52 + "\n")
    if "--no-browser" not in sys.argv:
        threading.Thread(target=open_browser_when_ready, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
