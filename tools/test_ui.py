"""Headless smoke test of the browser UI.

Run:  .venv\\Scripts\\python.exe tools/test_ui.py

Loads the real page in the same Chromium the exporter uses and drives the parts
that are easy to break and hard to notice: every template pack's render.js must
import and register, the home screen must list projects, opening one must build
the timeline, and placing one of each template must not throw. Any console error
or page exception fails the run.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server as S  # noqa: E402

IGNORED = (
    "favicon",
    "Failed to load resource",
)


def start_server() -> None:
    import uvicorn
    from urllib.request import urlopen

    def alive() -> bool:
        try:
            with urlopen(f"http://127.0.0.1:{S.PORT}/api/health", timeout=0.4) as response:
                return response.status == 200
        except Exception:
            return False

    if alive():
        return
    S.scan_templates()
    threading.Thread(
        target=lambda: uvicorn.run(S.app, host="127.0.0.1", port=S.PORT, log_level="error"),
        daemon=True,
    ).start()
    for _ in range(80):
        if alive():
            return
        time.sleep(0.15)
    raise RuntimeError("the test server did not start")


def build_project(name: str) -> Path:
    folder = S.project_dir(name)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "assets").mkdir(exist_ok=True)
    (folder / "exports").mkdir(exist_ok=True)
    source = folder / "source.mp4"
    S.run([S.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=20",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=20",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(source)], check=True)
    S.run([S.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "color=c=0x4ECDC4:s=640x420", "-frames:v", "1",
           str(folder / "assets" / "photo.png")], check=True)
    srt = ("1\n00:00:00,000 --> 00:00:03,000\nthe whole trick is compression\n\n"
           "2\n00:00:03,000 --> 00:00:06,000\nand that is the entire idea\n")
    state = S.ProjectState(
        name=name, source=source.name, source_info=S.probe(source),
        cuts=[S.Cut(src_in=0, src_out=20)],
        captions=S.parse_transcript(srt, "captions.srt"),
    )
    S.save_state(state)
    return folder


async def run_checks(project: str) -> None:
    from playwright.async_api import async_playwright

    problems: list[str] = []
    step = 0

    def note(label: str) -> None:
        nonlocal step
        step += 1
        print(f"  {step:2d}. {label}")

    executable = S.renderer_executable()
    if not executable:
        raise RuntimeError("Playwright Chromium is missing; run launch.cmd once")

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(executable_path=str(executable))
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
        page.on("console", lambda message: problems.append(f"console: {message.text}")
                if message.type == "error"
                and not any(skip in message.text for skip in IGNORED) else None)
        try:
            await page.goto(f"http://127.0.0.1:{S.PORT}/", wait_until="networkidle")
            await page.wait_for_function("window.editoro && Object.keys(window.editoro.store.packs).length > 0",
                                         timeout=15000)
            packs = await page.evaluate("Object.keys(editoro.store.packs).length")
            registered = await page.evaluate("Object.keys(editoro.renderers).length")
            assert packs >= 30, f"only {packs} packs reached the browser"
            assert registered >= packs, (
                f"{packs} packs declared but only {registered} renderers registered; "
                "a render.js failed to import")
            note(f"{packs} packs loaded, {registered} renderers registered")

            diagnostics = await page.text_content("#homeDiag")
            assert "Needs attention" not in (diagnostics or ""), diagnostics
            note(f"home screen diagnostics: {diagnostics.strip()}")

            await page.evaluate("name => editoro.openProject(name)", project)
            await page.wait_for_function("window.editoro && editoro.state() && editoro.state().source", timeout=15000)
            await page.wait_for_timeout(600)
            note("project opened, source and waveform loaded")

            cards = await page.eval_on_selector_all("#catsPane .catcard", "nodes => nodes.length")
            assert cards >= 30, f"template palette only rendered {cards} cards"
            note(f"template palette shows {cards} categories")

            # Place one of every template. This is the check that catches a
            # renderer which throws only once it has a real instance to draw.
            placed = await page.evaluate("""() => {
              const { store: S, placeInstance } = window.editoro;
              const ids = Object.keys(S.packs).filter(id => id !== 'captions');
              ids.forEach((id, index) => { S.t = 0.4 + index * 0.2; placeInstance(id); });
              return S.state.instances.length;
            }""")
            await page.wait_for_timeout(400)
            assert placed >= 30, f"only {placed} blocks placed"
            note(f"placed {placed} blocks from the palette")

            await page.evaluate("() => { editoro.store.t = 1.0; editoro.drawOverlay(); editoro.drawTimeline(); }")
            await page.wait_for_timeout(500)
            painted = await page.evaluate("""() => {
              const canvas = document.querySelector('#overlay');
              const ctx = canvas.getContext('2d');
              const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
              let lit = 0;
              for (let i = 3; i < data.length; i += 4 * 97) if (data[i] > 8) lit++;
              return lit;
            }""")
            assert painted > 0, "the overlay canvas is empty with 36 blocks on screen"
            note(f"overlay canvas painted ({painted} sampled pixels have ink)")

            # Orientation switch: both layouts must exist and differ.
            before = await page.evaluate(
                "editoro.state().instances.find(i => i.template === 'image-pop').x")
            await page.evaluate("() => editoro.cycleOrientation()")
            await page.evaluate("() => editoro.cycleOrientation()")
            await page.wait_for_timeout(300)
            vertical = await page.evaluate(
                "[editoro.state().orientation, editoro.state().instances.find(i => i.template==='image-pop').x]")
            assert vertical[0] == "vertical", vertical
            assert abs(vertical[1] - before) > 0.01, (
                "switching to 9:16 did not move the block to its vertical layout")
            await page.evaluate("() => editoro.cycleOrientation()")
            await page.wait_for_timeout(300)
            restored = await page.evaluate(
                "editoro.state().instances.find(i => i.template === 'image-pop').x")
            assert abs(restored - before) < 0.001, "the 16:9 layout was not restored"
            note("orientation cycles 16:9 -> 9:16 -> auto and restores each layout")

            await page.evaluate("() => document.querySelector('#captionStyleBtn').click()")
            await page.wait_for_timeout(200)
            assert await page.is_visible("#captionDlg"), "the caption dialog did not open"
            await page.evaluate(
                '''() => document.querySelector('#captionDlg [data-preset="center"]').click()''')
            await page.wait_for_timeout(200)
            centred = await page.evaluate("editoro.state().caption_style[editoro.state().orientation].y")
            assert abs(centred - 0.5) < 1e-6, centred
            await page.evaluate("() => document.querySelector('#capClose').click()")
            note("caption dialog opens and repositions captions")

            selected = await page.evaluate("""() => {
              const { store: S, renderInspector } = window.editoro;
              const block = S.state.captions[0];
              S.t = block.start + 0.1;
              S.sel = block.id;
              renderInspector();
              return document.querySelector('#inspPane [data-action="follow"]') !== null;
            }""")
            assert selected, "the caption inspector has no follow-global control"
            note("caption inspector exposes the follow-global toggle")

            undone = await page.evaluate("""() => {
              const { store: S, snapshot, afterMutate, undo } = window.editoro;
              const before = S.state.instances.length;
              snapshot();
              S.state.instances.pop();
              afterMutate();
              undo();
              return [before, S.state.instances.length];
            }""")
            assert undone[0] == undone[1], undone
            note("undo restores a deleted block")
        finally:
            await browser.close()

    if problems:
        raise AssertionError("browser reported errors:\n  - " + "\n  - ".join(problems[:12]))


def main() -> int:
    start_server()
    project = f"ui-selftest-{uuid.uuid4().hex[:8]}"
    folder = build_project(project)
    print(f"Editoro UI smoke test ({project})")
    try:
        asyncio.run(run_checks(project))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("\nUI smoke test OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
