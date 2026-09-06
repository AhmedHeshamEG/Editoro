"""Candidate counting sounds for the stat-pop template.

The one shipped (count-tick.wav) reads as a UI beep, which is wrong for a
counting number on a paper card: a number climbing should sound like a
mechanical counter or a pencil, not like a notification. These are candidates
to audition, written at the same 48 kHz mono PCM16 the rest of the library uses
so any of them can be dropped straight into templates/_shared/sfx/.

Everything here is synthesised rather than sourced, which means there is no
licence attached to any of it and nothing to credit.
"""
import wave
from pathlib import Path

import numpy as np

RATE = 48000
OUT = Path("templates/_shared/sfx/candidates")


def write(name: str, samples: np.ndarray) -> None:
    peak = float(np.max(np.abs(samples))) or 1.0
    # Conservative normalisation, matching the library: these play under speech
    # and must never be the loudest thing in the mix.
    data = np.clip(samples / peak * 0.72, -1, 1)
    OUT.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT / name), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes((data * 32767).astype("<i2").tobytes())
    print(f"  {name}  {len(samples) / RATE * 1000:.0f} ms")


def envelope(length: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(length) / RATE
    rise = np.clip(t / max(1e-6, attack), 0, 1)
    fall = np.exp(-t / max(1e-6, decay))
    return rise * fall


def noise(length: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, 1, length)


def lowpass(signal: np.ndarray, cutoff: float) -> np.ndarray:
    """One-pole, which is all a click needs; a click has no stopband to speak of."""
    a = np.exp(-2 * np.pi * cutoff / RATE)
    out = np.zeros_like(signal)
    previous = 0.0
    for index, value in enumerate(signal):
        previous = value * (1 - a) + previous * a
        out[index] = previous
    return out


def highpass(signal: np.ndarray, cutoff: float) -> np.ndarray:
    return signal - lowpass(signal, cutoff)


def wood(seed: int = 7) -> np.ndarray:
    """A wooden tick: two close resonances struck once, gone in 40 ms.

    This is the one that sounds like a counter wheel. The pair of frequencies
    matters more than either of them - a single sine reads as a beep, two
    slightly detuned ones read as a struck object.
    """
    length = int(RATE * 0.045)
    t = np.arange(length) / RATE
    body = (np.sin(2 * np.pi * 1180 * t) * 0.7
            + np.sin(2 * np.pi * 1790 * t) * 0.45
            + np.sin(2 * np.pi * 2630 * t) * 0.2)
    strike = highpass(noise(length, seed), 2200) * 0.6 * np.exp(-t / 0.0022)
    return (body * envelope(length, 0.0004, 0.0085)) + strike


def mechanical(seed: int = 11) -> np.ndarray:
    """A counter detent: a hard, dry, almost pitchless click with a tiny rattle."""
    length = int(RATE * 0.038)
    t = np.arange(length) / RATE
    click = lowpass(highpass(noise(length, seed), 1400), 7000)
    click *= np.exp(-t / 0.0035)
    body = np.sin(2 * np.pi * 640 * t) * 0.35 * np.exp(-t / 0.006)
    rattle = highpass(noise(length, seed + 1), 3500) * 0.18 * np.exp(-(t - 0.010) ** 2 / 2e-6)
    return click + body + rattle


def pencil(seed: int = 23) -> np.ndarray:
    """Graphite on paper: soft, broadband, no pitch at all. The quietest option."""
    length = int(RATE * 0.055)
    t = np.arange(length) / RATE
    grain = highpass(noise(length, seed), 1800)
    return grain * np.exp(-t / 0.010) * (1 - np.exp(-t / 0.0008))


def swell(seconds: float = 1.0) -> np.ndarray:
    """No ticking at all: one soft rise that lands with the number.

    For the version of this you asked about where the count is felt rather than
    counted. Played once per block instead of once per digit, so it needs a
    different `sfx` entry - no repeat_spacing, offset at the start of the count.
    """
    length = int(RATE * seconds)
    t = np.arange(length) / RATE
    k = t / seconds
    # Rising in pitch and in level together; the ear reads that as approach.
    frequency = 210 + 240 * k ** 1.7
    phase = 2 * np.pi * np.cumsum(frequency) / RATE
    tone = np.sin(phase) * 0.6 + np.sin(phase * 2) * 0.18
    air = lowpass(noise(length, 31), 900) * 0.25 * k
    shape = (k ** 1.4) * np.exp(-np.clip(k - 0.92, 0, 1) / 0.03)
    return (tone + air) * shape


def counter_run() -> np.ndarray:
    """Twelve wooden ticks in a row, so you can hear the repeat rather than one hit.

    Auditioning a counting sound one click at a time tells you almost nothing;
    what matters is whether twelve of them in a second sound like a mechanism or
    like a woodpecker.
    """
    spacing = int(RATE * 0.075)
    single = wood()
    out = np.zeros(spacing * 11 + len(single))
    for index in range(12):
        start = spacing * index
        out[start:start + len(single)] += single * (1 - index * 0.03)
    return out


if __name__ == "__main__":
    print("writing candidates to", OUT)
    write("count-wood.wav", wood())
    write("count-mechanical.wav", mechanical())
    write("count-pencil.wav", pencil())
    write("count-swell.wav", swell())
    write("DEMO-twelve-wood-ticks.wav", counter_run())
