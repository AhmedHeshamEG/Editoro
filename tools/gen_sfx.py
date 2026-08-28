"""Build Editoro's shared foley library.

Run:  .venv\\Scripts\\python.exe tools/gen_sfx.py

Five sounds are real recordings that ship with the repository (see
templates/SFX_SOURCES.md); the rest are synthesised from physical models -
granular crackle for paper, damped resonant modes for clicks and impacts,
swept-band noise for movement. Everything then goes through one calibration
pass, so `gain: 0.5` means the same perceived level in every pack.

Output: templates/_shared/sfx/*.wav (48 kHz, 16-bit, mono)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import audio_kit as ak  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "templates" / "_shared" / "sfx"

# Recordings already in the repository, kept and re-calibrated rather than replaced.
RECORDINGS = {
    "paper-slide.wav": "image-pop/paper-slide.wav",
    "paper-land.wav": "video-clip/paper-land.wav",
    "tactile-click.wav": "keyword/tactile-click.wav",
    "marker-stroke.wav": "highlight/dry-stroke.wav",
    "ui-settle.wav": "screen/ui-settle.wav",
}


# --------------------------------------------------------------- synthesis
def paper_drop() -> np.ndarray:
    """A sheet landing on a desk: body thump, then the sheet settling."""
    body = ak.modes(0.30, [78, 132, 214], [0.045, 0.032, 0.020], [1.0, 0.5, 0.22])
    body *= ak.decay(0.30, 0.05, 1.2)
    crackle = ak.grains(0.34, "paper-drop", np.linspace(2400, 260, 64),
                        band=(1400, 6200), grain_ms=2.0)
    tail = ak.grains(0.22, "paper-drop-tail", 220, band=(900, 3800), grain_ms=3.4)
    out = np.zeros(int(ak.SR * 0.42))
    out = ak.place(out, body * 0.9, 0.0)
    out = ak.place(out, crackle * 0.55, 0.004)
    out = ak.place(out, tail * 0.20, 0.13)
    return ak.fade(out, 1.5, 60)


def page_turn() -> np.ndarray:
    """A page swinging over: air movement with the sheet flexing inside it."""
    air = ak.noise(0.42, "page-air")
    air = ak.sweep_bandpass(air, 420, 2600, q=0.85, curve=0.8) * ak.bell(0.42, 0.42)
    flex = ak.grains(0.42, "page-flex",
                     np.concatenate([np.linspace(120, 1500, 40), np.linspace(1500, 180, 24)]),
                     band=(1600, 7200), grain_ms=2.2)
    snap = ak.modes(0.09, [1850, 3300], [0.010, 0.006], [0.5, 0.25]) * ak.decay(0.09, 0.012)
    out = np.zeros(int(ak.SR * 0.48))
    out = ak.place(out, air * 0.85, 0.0)
    out = ak.place(out, flex * 0.45, 0.02)
    out = ak.place(out, snap * 0.35, 0.30)
    return ak.fade(out, 6, 70)


def paper_tear() -> np.ndarray:
    """Fibres letting go one after another: grain density ramps, then stops."""
    density = np.concatenate([np.linspace(400, 5200, 48), np.linspace(5200, 900, 12)])
    body = ak.grains(0.36, "tear", density, band=(2200, 9000), grain_ms=1.6, jitter=1.1)
    low = ak.grains(0.36, "tear-low", density * 0.35, band=(700, 2200), grain_ms=3.0)
    out = ak.fade(body * 0.9 + low * 0.35, 4, 45)
    return out


def soft_click() -> np.ndarray:
    """A small, dry, non-plastic click - a fingernail on card, not a UI beep."""
    body = ak.modes(0.07, [1420, 2650, 4300], [0.009, 0.006, 0.004], [1.0, 0.45, 0.2])
    edge = ak.bandpass(ak.noise(0.02, "soft-click"), 3600, 1.6) * ak.decay(0.02, 0.0035)
    out = np.zeros(int(ak.SR * 0.09))
    out = ak.place(out, body, 0.0)
    out = ak.place(out, edge * 0.5, 0.0)
    return ak.fade(out, 0.6, 24)


def tick() -> np.ndarray:
    """Tiny neutral tick for counters and staggered list items."""
    body = ak.modes(0.035, [2400, 5100], [0.0045, 0.003], [1.0, 0.35])
    edge = ak.bandpass(ak.noise(0.008, "tick"), 5200, 2.0) * ak.decay(0.008, 0.0015)
    out = ak.place(np.zeros(int(ak.SR * 0.055)), body, 0.0)
    out = ak.place(out, edge, 0.0, 0.6)
    return ak.fade(out, 0.4, 16)


def pencil_tick() -> np.ndarray:
    """One short graphite mark - used by the annotation packs."""
    body = ak.grains(0.13, "pencil", np.linspace(2600, 900, 24), band=(1500, 6000), grain_ms=2.6)
    return ak.fade(body * 0.9, 3, 40)


def pop() -> np.ndarray:
    """A rising blip for things that appear rather than land."""
    t = ak.seconds(0.10)
    sweep = np.sin(2 * np.pi * (420 + 1500 * np.clip(t / 0.045, 0, 1) ** 0.7) * t)
    body = sweep * ak.decay(0.10, 0.022, 1.3)
    air = ak.bandpass(ak.noise(0.05, "pop"), 4200, 1.2) * ak.decay(0.05, 0.008) * 0.35
    out = np.zeros(int(ak.SR * 0.12))
    out = ak.place(out, body, 0.0)
    out = ak.place(out, air, 0.0)
    return ak.fade(out, 0.8, 34)


def whoosh(length: float, low: float, high: float, seed: str, skew: float = 0.45) -> np.ndarray:
    air = ak.noise(length, seed)
    swept = ak.sweep_bandpass(air, low, high, q=0.75, curve=0.85)
    swept = swept * ak.bell(length, skew)
    body = ak.lowpass(ak.noise(length, seed + "-body"), 380) * ak.bell(length, skew) * 0.25
    return ak.fade(swept * 0.9 + body, 8, length * 1000 * 0.35)


def thud_soft() -> np.ndarray:
    """Weighted low impact without a click on top - camera and card landings."""
    body = ak.modes(0.24, [62, 96, 155], [0.055, 0.035, 0.020], [1.0, 0.45, 0.18])
    body *= ak.decay(0.24, 0.06, 1.1)
    air = ak.lowpass(ak.noise(0.12, "thud"), 900) * ak.decay(0.12, 0.02) * 0.3
    out = np.zeros(int(ak.SR * 0.30))
    out = ak.place(out, body, 0.0)
    out = ak.place(out, air, 0.0)
    return ak.fade(out, 1.0, 70)


def shutter() -> np.ndarray:
    """Two-stage mechanical click for freeze frames."""
    first = ak.modes(0.05, [1750, 3400], [0.006, 0.004], [1.0, 0.4]) * ak.decay(0.05, 0.007)
    second = ak.modes(0.07, [1250, 2600], [0.010, 0.006], [0.9, 0.35]) * ak.decay(0.07, 0.011)
    out = np.zeros(int(ak.SR * 0.16))
    out = ak.place(out, first, 0.0)
    out = ak.place(out, second, 0.052)
    return ak.fade(out, 0.6, 40)


def stamp() -> np.ndarray:
    """Rubber stamp onto paper - impact plus the sheet answering underneath."""
    impact = ak.modes(0.16, [140, 260, 480], [0.020, 0.014, 0.008], [1.0, 0.5, 0.2])
    impact *= ak.decay(0.16, 0.025, 1.2)
    sheet = ak.grains(0.18, "stamp", np.linspace(1800, 200, 32), band=(1200, 5200), grain_ms=2.2)
    out = np.zeros(int(ak.SR * 0.24))
    out = ak.place(out, impact * 0.95, 0.0)
    out = ak.place(out, sheet * 0.4, 0.006)
    return ak.fade(out, 0.8, 55)


def chime_soft() -> np.ndarray:
    """A gentle, short two-note bell - used only where a section really opens."""
    first = ak.modes(0.9, [784, 1568, 2350], [0.32, 0.18, 0.10], [1.0, 0.30, 0.10])
    second = ak.modes(0.9, [1046, 2093, 3140], [0.30, 0.16, 0.09], [0.7, 0.22, 0.07])
    out = np.zeros(int(ak.SR * 1.25))
    out = ak.place(out, first, 0.0)
    out = ak.place(out, second, 0.13)
    return ak.fade(out, 4, 260)


def latch() -> np.ndarray:
    """Small mechanical seat - the sound of something snapping into place."""
    click = ak.modes(0.06, [980, 2150, 3900], [0.008, 0.005, 0.003], [1.0, 0.5, 0.2])
    body = ak.modes(0.14, [180, 320], [0.022, 0.014], [0.5, 0.2]) * ak.decay(0.14, 0.03)
    out = np.zeros(int(ak.SR * 0.18))
    out = ak.place(out, click, 0.0)
    out = ak.place(out, body * 0.6, 0.004)
    return ak.fade(out, 0.6, 48)


def tape_peel() -> np.ndarray:
    """Adhesive releasing - a dense, slightly pitched crackle that thins out."""
    density = np.concatenate([np.linspace(3800, 2400, 30), np.linspace(2400, 400, 26)])
    body = ak.grains(0.40, "peel", density, band=(2600, 9500), grain_ms=1.4, jitter=1.2)
    body = ak.peaking(body, 4200, 1.1, 2.5)
    return ak.fade(body * 0.85, 5, 90)


def swell() -> np.ndarray:
    """Low, slow rise used under chapter and hook cards. No melody, just lift."""
    air = ak.noise(1.4, "swell")
    body = ak.sweep_bandpass(air, 90, 640, q=0.6, curve=1.4) * ak.bell(1.4, 0.82)
    hum = ak.modes(1.4, [110, 165], [0.9, 0.7], [0.35, 0.12]) * ak.bell(1.4, 0.8)
    return ak.fade(body * 0.9 + hum, 120, 300)


SYNTHESISED = {
    "paper-drop.wav": paper_drop,
    "page-turn.wav": page_turn,
    "paper-tear.wav": paper_tear,
    "soft-click.wav": soft_click,
    "tick.wav": tick,
    "pencil-tick.wav": pencil_tick,
    "pop.wav": pop,
    "whoosh-short.wav": lambda: whoosh(0.26, 500, 3400, "whoosh-short", 0.42),
    "whoosh-long.wav": lambda: whoosh(0.62, 260, 4200, "whoosh-long", 0.5),
    "thud-soft.wav": thud_soft,
    "shutter.wav": shutter,
    "stamp.wav": stamp,
    "chime-soft.wav": chime_soft,
    "latch.wav": latch,
    "tape-peel.wav": tape_peel,
    "count-tick.wav": lambda: ak.fade(
        ak.modes(0.05, [1750, 3500], [0.006, 0.004], [1.0, 0.3]) * ak.decay(0.05, 0.008), 0.5, 22),
    "swell.wav": swell,
}


def main() -> int:
    SHARED.mkdir(parents=True, exist_ok=True)
    built: list[tuple[str, float, float, float]] = []

    for name, source in RECORDINGS.items():
        path = ROOT / "templates" / source
        if not path.is_file():
            print(f"[skip] recording missing: {source}")
            continue
        audio = ak.trim_silence(ak.read_wav(path))
        audio = ak.calibrate(ak.fade(audio, 2.0, 24.0))
        ak.write_wav(SHARED / name, audio)
        built.append((name, audio.size / ak.SR, ak.momentary_max_lufs(audio), ak.true_peak_db(audio)))

    for name, make in SYNTHESISED.items():
        audio = ak.calibrate(make())
        ak.write_wav(SHARED / name, audio)
        built.append((name, audio.size / ak.SR, ak.momentary_max_lufs(audio), ak.true_peak_db(audio)))

    # A 40 ms click physically cannot reach the loudness target without clipping:
    # its crest factor is enormous. So each sound lands on whichever of the two
    # limits binds first, and only a sound that hits neither is actually wrong.
    print(f"{'file':<22}{'sec':>7}{'LUFS-M':>9}{'dBTP':>8}  limited by")
    bad: list[str] = []
    for name, length, lufs, peak in sorted(built):
        at_loudness = abs(lufs - ak.TARGET_LUFS) <= 1.5
        at_ceiling = abs(peak - ak.TRUE_PEAK_CEILING) <= 0.25
        bound = "loudness" if at_loudness else "true peak" if at_ceiling else "NEITHER"
        if not (at_loudness or at_ceiling):
            bad.append(name)
        print(f"{name:<22}{length:>7.3f}{lufs:>9.2f}{peak:>8.2f}  {bound}")
    if bad:
        print(f"\n[warn] uncalibrated: {', '.join(bad)}")
        return 1
    print(f"\n{len(built)} sounds calibrated to {ak.TARGET_LUFS:.1f} LUFS-M "
          f"or {ak.TRUE_PEAK_CEILING:.1f} dBTP -> {SHARED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
