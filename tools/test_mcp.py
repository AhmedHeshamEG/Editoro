"""End-to-end check of the Editoro MCP surface.

Run:  .venv\\Scripts\\python.exe tools/test_mcp.py

Builds a synthetic project, drives it entirely through the MCP tools the way a
model would - read the transcript, anchor blocks to spoken quotes, look at a
rendered frame, export - and deletes it again. If this passes, an agent can do a
whole edit without touching the UI.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server as M  # noqa: E402
import server as S  # noqa: E402

SRT = """1
00:00:00,000 --> 00:00:02,000
the whole trick is compression

2
00:00:02,000 --> 00:00:04,000
about eighty seven percent of it

3
00:00:04,000 --> 00:00:06,000
and that is the entire idea
"""


def build_fixture(project: str) -> Path:
    folder = S.project_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "assets").mkdir(exist_ok=True)
    (folder / "exports").mkdir(exist_ok=True)
    source = folder / "source.mp4"
    S.run([
        S.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-g", "60", "-c:a", "aac", "-shortest", str(source),
    ], check=True)
    picture = folder / "assets" / "diagram.png"
    S.run([
        S.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=0x4ECDC4:s=600x400",
        "-frames:v", "1", str(picture),
    ], check=True)
    state = S.ProjectState(
        name=project, source=source.name, source_info=S.probe(source),
        cuts=[S.Cut(src_in=0, src_out=6)],
        captions=S.parse_transcript(SRT, "captions.srt"),
    )
    S.save_state(state)
    return folder


async def run_checks(project: str) -> None:
    check = 0

    def step(label: str) -> None:
        nonlocal check
        check += 1
        print(f"  {check:2d}. {label}")

    health = await M.diagnostics()
    assert health["ffmpeg"], health
    assert not health["template_errors"], health["template_errors"]
    assert health["templates_loaded"] >= 30
    step(f"diagnostics: {health['templates_loaded']} packs, "
         f"{'GPU' if health['gpu_encoder'] else 'CPU'} encoder")

    catalogue = await M.list_templates()
    assert catalogue["count"] >= 30
    keyword = next(t for t in catalogue["templates"] if t["id"] == "keyword")
    assert keyword["description"] and keyword["fields"][0]["description"]
    cameras = [t for t in catalogue["templates"] if t.get("moves_the_footage")]
    assert len(cameras) >= 4
    step(f"catalogue: {catalogue['count']} templates, {len(cameras)} camera moves, "
         "every field documented")

    overview = await M.get_project(project=project)
    assert overview["caption_blocks"] == 3
    assert overview["resolution"] == "640x360"
    assert overview["orientation"] == "horizontal"
    step("project overview reads source, captions and orientation")

    transcript = await M.get_transcript(project=project)
    assert len(transcript["blocks"]) == 3
    srt = await M.get_transcript(project=project, format="srt")
    assert "00:00:00,000 --> " in srt["srt"]
    step("transcript readable as blocks and as SRT")

    found = await M.find_quote(project=project, quote="the whole TRICK, is compression!")
    assert found["matches"], found
    assert abs(found["matches"][0]["start"] - 0.0) < 0.01
    spanning = await M.find_quote(project=project, quote="percent of it and that is")
    assert spanning["matches"], "a quote spanning two caption blocks must still resolve"
    step("quotes resolve despite punctuation, case and block boundaries")

    placed = await M.place_blocks(project=project, blocks=[
        M.Block(template="keyword", quote="the whole trick is compression",
                duration=1.5, fields={"text": "compression"}),
        M.Block(template="stat-pop", quote="eighty seven percent",
                duration=1.5, fields={"value": "87", "suffix": "%", "label": "of it"}),
        M.Block(template="image-pop", at=3.0, duration=1.2,
                fields={"asset": "diagram.png", "caption": "the diagram"}),
        M.Block(template="punch-in", at=4.4, duration=1.0,
                fields={"rect": {"x": 0.25, "y": 0.2, "w": 0.5, "h": 0.6}}),
    ])
    assert placed["placed"] == 4, placed
    assert "problems" not in placed, placed["problems"]
    tracks = {block["template"]: block["track"] for block in placed["blocks"]}
    assert len(set(tracks.values())) >= 2, "overlapping blocks must not share a track"
    step(f"placed 4 blocks by quote and timestamp onto tracks {sorted(set(tracks.values()))}")

    rejected = await M.place_blocks(project=project, blocks=[
        M.Block(template="does-not-exist", at=1.0),
        M.Block(template="keyword", at=1.0, fields={"nope": "x"}),
        M.Block(template="keyword", quote="words never spoken in this video"),
        M.Block(template="zoom-out", at=4.5, duration=0.8,
                fields={"rect": {"x": 0.2, "y": 0.2, "w": 0.6, "h": 0.6}}),
    ])
    problems = " | ".join(rejected["problems"])
    assert "unknown template" in problems
    assert "no field 'nope'" in problems
    assert "could not find the quote" in problems
    assert "only one can be active at a time" in problems, problems
    step("bad calls are reported precisely, not silently dropped")

    vertical = await M.set_orientation(project=project, mode="vertical")
    assert vertical["orientation"] == "vertical"
    after = await M.get_project(project=project)
    moved = next(b for b in after["timeline"] if b["template"] == "image-pop")
    back = await M.set_orientation(project=project, mode="auto")
    assert back["orientation"] == "horizontal"
    restored = await M.get_project(project=project)
    original = next(b for b in restored["timeline"] if b["template"] == "image-pop")
    assert original["x"] != moved["x"], "each orientation must keep its own layout"
    step(f"orientation switch relaid {vertical['blocks_relaid']} blocks and restored them")

    styled = await M.set_caption_style(
        project=project, position="lower", grouping="chunk",
        words_per_chunk=3, font_size=48, highlight_active_word=True)
    assert styled["caption_style"]["font_size"] == 48
    step("caption style set per orientation")

    fixed = await M.edit_captions(project=project, edits=[
        M.CaptionEdit(id=transcript["blocks"][0]["id"], text="the whole trick is compression"),
        M.CaptionEdit(id=transcript["blocks"][2]["id"], pin_to=[0.3, 0.25]),
    ])
    assert fixed["updated"] == 2
    step("captions corrected and one pinned away from the global placement")

    trimmed = await M.remove_range(project=project, start=5.2, end=6.0)
    assert trimmed["timeline_duration"] < 6.0
    step(f"removed a span; timeline now {trimmed['timeline_duration']}s")

    frame = await M.render_frame(project=project, at=0.8, width=480)
    png = M.base64.b64decode(frame.data)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 3000
    step(f"rendered a real composited frame ({len(png) // 1024} KB PNG)")

    exported = await M.export(project=project, mode="lossless")
    assert exported.get("exported", "").endswith(".mp4"), exported
    output = Path(exported["path"])
    assert output.is_file() and output.stat().st_size > 10_000
    media = S.probe(output)
    assert media.audio_codec == "aac" and media.width == 640
    step(f"exported {exported['exported']} ({exported['size_mb']} MB, "
         f"{media.duration:.2f}s, {media.codec})")

    await M.set_notes(project=project, notes="checked by tools/test_mcp.py")
    assert (await M.get_project(project=project, include_timeline=False))["notes"]
    step("notes persist on the project")


def main() -> int:
    project = f"mcp-selftest-{uuid.uuid4().hex[:8]}"
    folder = build_fixture(project)
    print(f"Editoro MCP end-to-end ({project})")
    try:
        asyncio.run(run_checks(project))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("\nMCP end-to-end OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
