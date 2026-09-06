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
    look = folder / "look"
    look.mkdir(exist_ok=True)
    source = folder / "source.mp4"
    S.run([S.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=20",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=20",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(source)], check=True)
    S.run([S.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "color=c=0x4ECDC4:s=640x420", "-frames:v", "1",
           str(folder / "assets" / "photo.png")], check=True)
    # A stand-in depth matte: near at the bottom of the frame, far at the top.
    # The real one comes out of the depth model, which is far too slow - and far
    # too dependent on a GPU - to belong in a smoke test. What is under test here
    # is the compositor, and it cannot tell where its matte came from.
    S.run([S.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "gradients=s=640x360:c0=black:c1=white:type=linear:"
           "x0=0:y0=360:x1=0:y1=0:d=20:r=30",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "30",
           "-pix_fmt", "yuv420p", str(look / "matte.mp4")], check=True)
    S.write_cube(look / "grade.cube", {
        "gain_r": 0.92, "gain_b": 1.06, "black": 0.02, "white": 0.94,
        "gamma": 1.05, "contrast": 0.15, "saturation": 1.1}, 1.0, 17)
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

            # A block's scale has to change the size of the block. Most packs
            # sized their outer box from `viewport.width * max_width`, which
            # does not scale, so past the point where the type filled that box
            # the size control did nothing at all - and for the packs with a
            # fixed-width card it did nothing from the start.
            sized = await page.evaluate("""() => {
              const { store: S, renderers: REG, runtime: A } = window.editoro;
              const viewport = { width: 1920, height: 1080 };
              const stuck = [], grew = [];
              for (const [id, fns] of Object.entries(REG)) {
                // Camera moves and the breathing block draw nothing of their
                // own: their "size" is a framing rectangle, and scale is not
                // theirs to honour.
                if (!fns.measure || S.packs[id]?.full_frame) continue;
                if (S.packs[id]?.camera || id === "breathe") continue;
                const item = S.state.instances.find(i => i.template === id);
                if (!item) continue;
                const at = k => fns.measure({ ...item, scale: k }, A, viewport);
                const one = at(1), two = at(2);
                if (!(one?.width > 0) || !(two?.width > 0)) continue;
                (two.width > one.width * 1.6 ? grew : stuck)
                  .push(`${id} ${one.width.toFixed(0)}->${two.width.toFixed(0)}`);
              }
              return { stuck, grew };
            }""")
            assert not sized["stuck"], (
                "doubling the scale did not resize these blocks: "
                + ", ".join(sized["stuck"]))
            note(f"scale resizes every measurable pack ({len(sized['grew'])} checked)")

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

            # A number that counts and a bar that fills are read, not felt, so
            # they must climb monotonically and still be climbing after the
            # entrance spring has settled. Riding the spring — which is what
            # they used to do — makes them snap to their value in two frames
            # and then wobble around it, which is not a count-up at all.
            ramp = await page.evaluate("""() => {
              const { store: S, motionAt, valueAt } = window.editoro;
              const inst = S.state.instances.find(i => i.template === "stat-pop");
              if (!inst) return null;
              const at = t => valueAt(inst, t, "number");
              const samples = [];
              for (let n = 0; n <= 40; n++) samples.push(at(n / 40));
              const monotonic = samples.every((v, n) => n === 0 || v >= samples[n - 1] - 1e-9);
              const bounded = samples.every(v => v >= 0 && v <= 1);
              // 200 ms in: the spring has effectively landed, the count has not.
              const early = 0.2 / inst.duration;
              return {
                monotonic, bounded,
                spring: motionAt(inst, early, "number").k,
                value: at(early),
                arrived: at(1),
              };
            }""")
            assert ramp, "no stat-pop instance was placed"
            assert ramp["monotonic"], "the count-up ramp goes backwards"
            assert ramp["bounded"], f"the count-up ramp leaves 0..1: {ramp}"
            assert ramp["arrived"] >= 0.999, f"the count never reaches its value: {ramp}"
            assert ramp["value"] < 0.9, (
                f"the count is already finished 200 ms in: {ramp}")
            note(f"count-up ramp climbs to {ramp['value']:.2f} at 200 ms "
                 f"where the spring is already at {ramp['spring']:.2f}")

            # The source camera. Every one of these used to be wrong on screen
            # and right on export, which is the worst way round: ken-burns
            # snapped to its start framing and sat there, and zoom-out played
            # backwards, because the preview knew only one move and applied it
            # to every camera pack regardless of the mode the pack declared.
            # _test_camera_modes() in server.py asserts the same shapes against
            # the FFmpeg expressions, so the two halves are pinned together.
            camera = await page.evaluate("""() => {
              const { cameraPlan, cameraStateAt } = window.editoro;
              const tight = { x: .22, y: .16, w: .56, h: .64 };
              const wide = { x: .10, y: .08, w: .80, h: .84 };
              const zoom = (template, fields, duration, when) => cameraStateAt(
                cameraPlan({ id: "c", template, start: 0, duration, fields }), when).scale;
              const kb = t => zoom("ken-burns", { rect: wide, rect2: tight }, 4, t);
              return {
                punchStart: zoom("punch-in", { rect: tight }, 3, 0),
                punchHold: zoom("punch-in", { rect: tight }, 3, 1.5),
                outStart: zoom("zoom-out", { rect: tight }, 3, 0),
                outEnd: zoom("zoom-out", { rect: tight }, 3, 2.9),
                burns: [kb(0), kb(2), kb(3.99)],
              };
            }""")
            assert camera["punchStart"] < 1.02, (
                f"a punch-in must start on the full frame: {camera['punchStart']:.3f}")
            assert camera["punchHold"] > 1.4, (
                f"a punch-in must hold tight in the middle: {camera['punchHold']:.3f}")
            assert camera["outStart"] > 1.4, (
                f"a zoom-out must start tight: {camera['outStart']:.3f}")
            assert camera["outEnd"] < 1.05, (
                f"a zoom-out must end on the full frame: {camera['outEnd']:.3f}")
            begin, middle, end = camera["burns"]
            assert begin < middle < end, (
                f"ken-burns must travel, not snap and hold: {camera['burns']}")
            note(f"camera modes: punch {camera['punchStart']:.2f}->{camera['punchHold']:.2f}, "
                 f"zoom-out {camera['outStart']:.2f}->{camera['outEnd']:.2f}, "
                 f"ken-burns {begin:.2f}->{end:.2f}")

            # Breathing: one clock, shared. The overlays have to be at the same
            # point in the breath as the footage at the same instant, because
            # the whole effect is that they move together; two clocks would
            # read as wobbling stickers on a steady video.
            breathing = await page.evaluate("""async () => {
              const { store: S, breatheScale, breatheAmplitude } = window.editoro;
              const before = S.state.breathe, blocks = S.state.instances;
              // Measured on a bare timeline. The palette check above placed one
              // of every pack, `breathe` included, and that block overriding
              // the project strength is the correct behaviour - it is just not
              // what this measurement is about.
              S.state.instances = [];
              const read = level => {
                S.state.breathe = level;
                const samples = [];
                for (let n = 0; n <= 60; n++) samples.push(breatheScale(n / 4));
                return { min: Math.min(...samples), max: Math.max(...samples),
                         amplitude: breatheAmplitude(0) };
              };
              const off = read("off"), subtle = read("subtle");
              const standard = read("standard"), strong = read("strong");
              S.state.breathe = before; S.state.instances = blocks;
              return { off, subtle, standard, strong };
            }""")
            assert breathing["off"]["max"] == 1 and breathing["off"]["min"] == 1, (
                "breathing off still moved the frame")
            # Never below 1: scaling the footage down would pull the edges of
            # the picture into shot.
            assert breathing["standard"]["min"] >= 1 - 1e-9, breathing["standard"]
            assert (breathing["subtle"]["amplitude"] < breathing["standard"]["amplitude"]
                    < breathing["strong"]["amplitude"]), breathing
            assert breathing["standard"]["max"] < 1.05, (
                f"breathing is far too deep to be invisible: {breathing['standard']}")
            note(f"breathing scales 1.000-{breathing['standard']['max']:.4f} at standard, "
                 f"and is genuinely flat when off")

            # Silence removes a block's foley from the mix AND its markers from
            # the SFX track, so that track keeps being a truthful picture of
            # what the export will contain - and `lite`, the default, is the
            # block without whatever opens it. The two ends have to be measured
            # from the same block: a control with three states is only worth
            # having if the middle one is neither of the other two.
            foley = await page.evaluate("""() => {
              const { store: S, sfxEventsForInstance } = window.editoro;
              const at = (i, mode) => sfxEventsForInstance(
                { ...i, foley: mode }, S.packs[i.template]).length;
              const inst = S.state.instances.find(
                i => (S.packs[i.template].sfx || []).some(x => x.lead));
              if (!inst) return null;
              return { off: at(inst, "off"), lite: at(inst, "lite"),
                       full: at(inst, "full"), template: inst.template };
            }""")
            assert foley, "no placed block declares an opening sound to drop"
            assert foley["off"] == 0 and 0 < foley["lite"] < foley["full"], (
                f"the three foley states are not three things: {foley}")
            note(f"a {foley['template']} block plays {foley['off']}, {foley['lite']} "
                 f"then {foley['full']} sounds across the foley cycle")

            # The breathe block's rectangle: it used to be paintable, draggable
            # and completely ignored, which is the one thing a control must
            # never be. Its centre is where the breath pulls and its edges are
            # how far it may go.
            pull = await page.evaluate("""() => {
              const { store: S, breatheFraming, framingRect } = window.editoro;
              const item = { id: "br", template: "breathe", start: 0, duration: 4,
                             scale: 1, fields: { strength: "strong" } };
              const wide = { ...item, fields: { strength: "strong",
                             rect: { x: .5, y: .1, w: .4, h: .5 } } };
              const tight = { ...item, fields: { strength: "strong",
                              rect: { x: .01, y: .01, w: .98, h: .98 } } };
              return { plain: breatheFraming(item, "horizontal"),
                       wide: breatheFraming(wide, "horizontal"),
                       tight: breatheFraming(tight, "horizontal"),
                       draggable: !!framingRect(wide) };
            }""")
            assert pull["draggable"], "the breathe rectangle is not a framing rectangle"
            assert pull["plain"][1] == 0.5 and pull["wide"][1] == 0.7, (
                f"the breathe rectangle does not move the pull: {pull}")
            assert pull["tight"][0] < pull["plain"][0] == pull["wide"][0], (
                f"the breathe rectangle is not a ceiling on the strength: {pull}")
            note(f"the breathe box pulls to {pull['wide'][1]:.2f} and caps strong "
                 f"from {pull['plain'][0]:.3f} to {pull['tight'][0]:.3f}")

            # The thumbnail. It is the one surface that draws at final size
            # rather than at preview size, and the PNG that gets saved comes out
            # of the same drawThumbnail() the dialog shows - so a canvas that
            # comes back empty here is a cover that would be uploaded empty.
            cover = await page.evaluate("""async () => {
              const { $, drawThumbnail, thumbState } = window.editoro;
              $("#thumbBtn").click();
              // Opening it fetches the base frame, so the dialog is not up yet;
              // its size is half of what is being checked here, so wait for it
              // rather than for a guess at how long a frame grab takes.
              const dialog = $("#thumbDlg");
              for (let i = 0; i < 300 && !dialog.open; i++)
                await new Promise(done => setTimeout(done, 100));
              await new Promise(done => requestAnimationFrame(done));
              const thumb = thumbState();
              const canvas = document.createElement("canvas");
              canvas.width = 1280; canvas.height = 720;
              const ctx = canvas.getContext("2d");
              drawThumbnail(ctx, 1280, 720, false);
              const pixels = ctx.getImageData(0, 0, 1280, 720).data;
              let bright = 0;
              for (let i = 0; i < pixels.length; i += 4)
                if (pixels[i] > 200 && pixels[i + 1] > 200) bright++;
              const layouts = thumb.elements.every(
                e => e.layouts?.horizontal && e.layouts?.vertical);
              // How the dialog itself came out. The cover is the one thing in
              // the editor whose whole job is to be looked at, and it was being
              // shown in a 380-pixel strip inside a dialog sized for a form.
              const shown = $("#thumbCanvas").getBoundingClientRect();
              const box = $("#thumbDlg").getBoundingClientRect();
              $("#thumbDlg").close();
              return { elements: thumb.elements.length, bright, layouts,
                       kinds: thumb.elements.map(e => e.kind),
                       shownW: Math.round(shown.width), shownH: Math.round(shown.height),
                       onScreen: box.top >= -1 && box.bottom <= innerHeight + 1
                                 && box.left >= -1 && box.right <= innerWidth + 1 };
            }""")
            assert cover["elements"] >= 2, f"the thumbnail seeded no elements: {cover}"
            assert cover["layouts"], "a thumbnail element is missing one of its two layouts"
            # The headline is white and enormous; if none of it landed, either
            # the font never loaded or the element was drawn off-canvas.
            assert cover["bright"] > 2000, (
                f"the thumbnail headline did not draw: {cover}")
            # The dialog has to hold the whole cover and stay on the screen.
            assert cover["onScreen"], f"the cover dialog does not fit the window: {cover}"
            assert cover["shownW"] >= 600, (
                f"the cover is shown too small to judge: {cover['shownW']}px wide")
            assert abs(cover["shownW"] / max(1, cover["shownH"]) - 16 / 9) < .02, (
                f"the cover preview is not the shape of the cover: {cover}")
            note(f"thumbnail draws {cover['elements']} elements "
                 f"({', '.join(cover['kinds'])}) at 1280x720, shown "
                 f"{cover['shownW']}x{cover['shownH']} on screen, both layouts kept")

            # The counting ladder. The whole point is that the ticking and the
            # digits are one schedule, so this reads them from opposite ends:
            # the tick times out of the foley, and the moments the drawn number
            # actually changes out of A.value(). They have to be the same list.
            # _test_value_ladder() in server.py holds the export side to it.
            counting = await page.evaluate("""() => {
              const { store: S, valueAt, valueLadder, sfxEventsForInstance } = window.editoro;
              const item = { id: "stat", template: "stat-pop", start: 0, duration: 3,
                             fields: { value: "250" }, scale: 1 };
              const ladder = valueLadder(item, "number");
              const ticks = sfxEventsForInstance(item, S.packs["stat-pop"])
                .filter(e => e.url.includes("count-wood")).map(e => e.time);
              // Walk the block frame by frame and note every moment the number
              // it would draw changes: that is the picture, not the plan.
              const changes = [];
              let previous = 0;
              for (let frame = 1; frame <= 90; frame++) {
                const seconds = frame / 30;
                const value = valueAt(item, seconds / item.duration, "number");
                if (value !== previous) { changes.push(seconds); previous = value; }
              }
              return { ladder, ticks, changes, final: valueAt(item, 1, "number") };
            }""")
            ladder, ticks = counting["ladder"], counting["ticks"]
            assert len(ladder) == 10, f"the stat ladder is not ten notches: {ladder}"
            assert ticks == ladder, (
                f"the ticking is not on the ladder: {ticks} vs {ladder}")
            gaps = [b - a for a, b in zip(ladder, ladder[1:])]
            assert all(b > a for a, b in zip(gaps, gaps[1:])), (
                f"the count does not decelerate: {gaps}")
            assert counting["final"] == 1, (
                f"the number never reaches its value: {counting['final']}")
            # Every notch shows up on screen, within the frame it is due on.
            assert len(counting["changes"]) == len(ladder), (
                f"drawn changes {counting['changes']} do not match ladder {ladder}")
            drift = max(abs(a - b) for a, b in zip(counting["changes"], ladder))
            assert drift <= 1 / 30 + 1e-6, f"a digit changed {drift:.3f}s off its tick"
            # "Increase by" moves the notch count from the pack to the block,
            # and the browser and the exporter have to agree about it or the
            # ticking lands on digits that are not there.
            by = await page.evaluate("""() => {
              const { store: S, valueSteps, valueLadder, sfxEventsForInstance } = window.editoro;
              const item = { id: "stat2", template: "stat-pop", start: 0, duration: 3,
                             fields: { value: "1,000", start: "0", step: "200" }, scale: 1 };
              return { steps: valueSteps(item), ladder: valueLadder(item, "number").length,
                       ticks: sfxEventsForInstance(item, S.packs["stat-pop"]).length };
            }""")
            assert by == {"steps": 5, "ladder": 5, "ticks": 5}, (
                f"counting up by 200 is not five notches: {by}")

            note(f"the count steps {len(ladder)} times, gaps "
                 f"{gaps[0] * 1000:.0f}ms->{gaps[-1] * 1000:.0f}ms, "
                 f"every tick on a digit change")

            # The look preview: switch both halves on and read the composited
            # canvas back. A WebGL chain that fails quietly still paints a
            # perfectly plausible black rectangle, so the only honest check is
            # to compare pixels against the same frame with the look off.
            looked = await page.evaluate("""async () => {
              const { store: S, lookRefresh, lookSample, LOOK } = window.editoro;
              // testsrc2 animates, and how much a blur changes depends on how
              // much detail the frame has. Pinning both videos to one moment
              // makes the three measurements comparable to each other and to
              // the same run tomorrow.
              const vid = document.querySelector("#vid");
              async function park() {
                // Paused, not merely seeked. lookSample() reads the composite
                // out of WebGL and then draws the same <video> onto a 2D canvas
                // to compare against; a video still running between those two
                // steps hands it two different frames of testsrc2, and the
                // difference it then reports is the seek, not the look.
                S.playing = false;
                for (const media of [vid, LOOK.matte]) {
                  if (!media.paused) media.pause();
                }
                for (const media of [vid, LOOK.matte]) {
                  if (!media.src || media.readyState < 1) continue;
                  // Both have to *arrive*, not just be asked. The matte is a
                  // gradient that moves through the clip, so a matte still
                  // sitting at zero while the footage is at five puts the
                  // near/far split somewhere else entirely and the defocus
                  // measures whatever that happens to blur.
                  const deadline = performance.now() + 5000;
                  while (Math.abs(media.currentTime - 5) > 0.002
                         && performance.now() < deadline) {
                    media.currentTime = 5;
                    await new Promise(done => {
                      media.addEventListener("seeked", done, { once: true });
                      setTimeout(done, 1000);
                    });
                  }
                }
              }
              async function measure(defocus, grade) {
                S.state.look = { ...S.state.look, defocus, grade,
                  matte_revision: S.state.source_revision,
                  grade_revision: S.state.source_revision };
                await lookRefresh();
                // The matte is a second <video>, and until it has decoded a
                // frame the compositor correctly declines to defocus anything.
                // Sampling before then measures the wait, not the look.
                const deadline = performance.now() + 8000;
                while (performance.now() < deadline) {
                  const ready = !defocus || LOOK.matte.readyState >= 2;
                  if (ready && LOOK.lutSize > (grade ? 2 : 0)) break;
                  await new Promise(done => setTimeout(done, 100));
                }
                await park();
                await new Promise(done => setTimeout(done, 400));
                return lookSample();
              }
              // Each half on its own, then both. Measuring only the two
              // together lets one of them be silently dead - which is exactly
              // how a grade that changed nothing survived here for a while.
              const defocusOnly = await measure(0.8, 0);
              const gradeOnly = await measure(0, 1);
              const both = await measure(0.8, 1);
              const lut = LOOK.lutSize, active = LOOK.active;
              S.state.look = { ...S.state.look, defocus: 0, grade: 0 };
              await lookRefresh();
              return { defocusOnly, gradeOnly, both, lut, active,
                       handedBack: !LOOK.active,
                       failed: LOOK.failed, error: LOOK.error };
            }""")
            assert not looked["failed"], f"the look preview failed to start: {looked['error']}"
            assert looked["active"], "the look preview never switched on"
            assert looked["both"]["ink"] > looked["both"]["width"] * looked["both"]["height"] * 0.5,                 f"the look canvas came back essentially black: {looked['both']}"
            assert looked["lut"] > 2, f"the grade table never uploaded: {looked['lut']}"
            assert looked["handedBack"], "switching the look off left the canvas up"
            # Each half has to be plainly visible on its own, not merely
            # non-zero. The first version of this asked for one code value out
            # of 255 of *average brightness* across the frame, and so passed
            # happily on a grade that was almost pure contrast and moved the
            # average not at all.
            # The fixture grade moves this frame about 2/255 per pixel, and it
            # is meant to: it is a colour grade, not a filter. This assertion
            # used to ask for 3, and passed only on the runs where the video was
            # still rolling between the two reads inside lookSample() - it was
            # measuring a seek. With that fixed the honest number is stable, and
            # the direction check below is what actually proves the grade ran.
            grade = looked["gradeOnly"]
            assert grade["diff"] > 1.2, (
                f"the grade is too subtle to see: {grade['diff']:.1f}/255 per pixel")
            # gain_r 0.92 against gain_b 1.06 in the fixture cube: blue has to
            # come up *relative to* red. Not in absolute terms - the same table
            # lifts the black point and rolls off the white, which pulls every
            # channel down a little - so the channel balance is the part that
            # only a grade that actually ran can produce. An identity table
            # moves the two together and fails this by construction.
            tilt = ((grade["blue"] - grade["rawBlue"])
                    - (grade["red"] - grade["rawRed"]))
            assert tilt > 1.0, f"the grade did not cool the frame: {tilt:.2f}, {grade}"
            assert looked["defocusOnly"]["diff"] > 3.0, (
                f"the defocus is too subtle to see: "
                f"{looked['defocusOnly']['diff']:.1f}/255 per pixel")
            shifted = looked["both"]["diff"]
            note(f"look preview composites defocus + grade "
                 f"({looked['lut']} cube; defocus {looked['defocusOnly']['diff']:.1f}, "
                 f"grade {looked['gradeOnly']['diff']:.1f} tilting blue "
                 f"{tilt:.1f} past red, "
                 f"both {shifted:.1f} /255 per pixel)")

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
