"""Deterministic foley synthesis and loudness calibration for Editoro template packs.

Everything here is offline, dependency-light (numpy + the standard library) and
seeded, so regenerating the sound design produces byte-identical files.

Loudness: every generated or imported sound is measured with an ITU-R BS.1770
K-weighted integrated loudness and normalised to one shared target, then true-peak
limited. That is what makes the pack `gain` values comparable instead of magic
numbers - a template asking for 0.5 gets the same perceived level in every pack.
"""
from __future__ import annotations

import hashlib
import math
import wave
from pathlib import Path

import numpy as np

SR = 48000
TARGET_LUFS = -20.0        # momentary-max target; sits under speech at pack gain 0.5
TRUE_PEAK_CEILING = -1.5   # dBTP


# ----------------------------------------------------------------- filters
def biquad(x: np.ndarray, b: tuple[float, float, float], a: tuple[float, float, float]) -> np.ndarray:
    """Transposed direct-form-II biquad, vectorised over the two recursive taps."""
    b0, b1, b2 = (c / a[0] for c in b)
    a1, a2 = a[1] / a[0], a[2] / a[0]
    y = np.empty_like(x)
    x1 = x2 = y1 = y2 = 0.0
    for n in range(x.size):
        xn = x[n]
        yn = b0 * xn + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        y[n] = yn
        x2, x1 = x1, xn
        y2, y1 = y1, yn
    return y


def _rbj(kind: str, freq: float, q: float, gain_db: float = 0.0):
    w0 = 2 * math.pi * freq / SR
    cos_w0, sin_w0 = math.cos(w0), math.sin(w0)
    alpha = sin_w0 / (2 * q)
    if kind == "lowpass":
        b = ((1 - cos_w0) / 2, 1 - cos_w0, (1 - cos_w0) / 2)
        a = (1 + alpha, -2 * cos_w0, 1 - alpha)
    elif kind == "highpass":
        b = ((1 + cos_w0) / 2, -(1 + cos_w0), (1 + cos_w0) / 2)
        a = (1 + alpha, -2 * cos_w0, 1 - alpha)
    elif kind == "bandpass":
        b = (alpha, 0.0, -alpha)
        a = (1 + alpha, -2 * cos_w0, 1 - alpha)
    elif kind == "peaking":
        amp = 10 ** (gain_db / 40)
        b = (1 + alpha * amp, -2 * cos_w0, 1 - alpha * amp)
        a = (1 + alpha / amp, -2 * cos_w0, 1 - alpha / amp)
    elif kind == "highshelf":
        amp = 10 ** (gain_db / 40)
        sqrt_term = 2 * math.sqrt(amp) * alpha
        b = (amp * ((amp + 1) + (amp - 1) * cos_w0 + sqrt_term),
             -2 * amp * ((amp - 1) + (amp + 1) * cos_w0),
             amp * ((amp + 1) + (amp - 1) * cos_w0 - sqrt_term))
        a = ((amp + 1) - (amp - 1) * cos_w0 + sqrt_term,
             2 * ((amp - 1) - (amp + 1) * cos_w0),
             (amp + 1) - (amp - 1) * cos_w0 - sqrt_term)
    else:
        raise ValueError(kind)
    return b, a


def lowpass(x, freq, q=0.707):
    return biquad(x, *_rbj("lowpass", min(freq, SR * 0.45), q))


def highpass(x, freq, q=0.707):
    return biquad(x, *_rbj("highpass", max(freq, 10.0), q))


def bandpass(x, freq, q=1.0):
    return biquad(x, *_rbj("bandpass", min(max(freq, 20.0), SR * 0.45), q))


def peaking(x, freq, q, gain_db):
    return biquad(x, *_rbj("peaking", freq, q, gain_db))


def sweep_bandpass(x: np.ndarray, start_hz: float, end_hz: float, q: float = 1.4,
                   curve: float = 1.0) -> np.ndarray:
    """Time-varying bandpass, computed blockwise with overlap so it stays smooth."""
    block = 256
    out = np.zeros_like(x)
    for start in range(0, x.size, block):
        end = min(x.size, start + block)
        position = (start / max(1, x.size - 1)) ** curve
        freq = start_hz * (end_hz / start_hz) ** position
        chunk_start = max(0, start - block)
        chunk_end = min(x.size, end + block)
        filtered = bandpass(x[chunk_start:chunk_end].copy(), freq, q)
        out[start:end] += filtered[start - chunk_start:end - chunk_start]
    return out


# ------------------------------------------------------------- generators
def rng(seed: str) -> np.random.Generator:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "little"))


def seconds(length: float) -> np.ndarray:
    return np.arange(max(1, int(SR * length))) / SR


def noise(length: float, seed: str) -> np.ndarray:
    return rng(seed).standard_normal(max(1, int(SR * length)))


def decay(length: float, tau: float, power: float = 1.0) -> np.ndarray:
    return np.exp(-seconds(length) / tau) ** power


def bell(length: float, skew: float = 0.35) -> np.ndarray:
    """Attack/release amplitude bell with its peak at `skew` of the length."""
    t = np.linspace(0.0, 1.0, max(2, int(SR * length)))
    up = np.clip(t / max(skew, 1e-4), 0, 1) ** 1.6
    down = np.clip((1 - t) / max(1 - skew, 1e-4), 0, 1) ** 1.9
    return up * down


def modes(length: float, freqs, taus, amps) -> np.ndarray:
    """Sum of exponentially decaying sinusoids - a struck resonant body."""
    t = seconds(length)
    out = np.zeros_like(t)
    for freq, tau, amp in zip(freqs, taus, amps):
        out += amp * np.sin(2 * math.pi * freq * t) * np.exp(-t / tau)
    return out


def grains(length: float, seed: str, density, band=(1800.0, 7000.0),
           grain_ms: float = 2.4, jitter: float = 0.8) -> np.ndarray:
    """Granular crackle - the physical model behind paper, tearing and writing.

    `density` is a constant or a curve of grains-per-second across the sound.
    """
    generator = rng(seed)
    total = max(1, int(SR * length))
    out = np.zeros(total + SR // 10)
    density = np.asarray(density, dtype=float)
    if density.ndim == 0:
        density = np.full(total, float(density))
    else:
        density = np.interp(np.linspace(0, 1, total), np.linspace(0, 1, density.size), density)
    grain_len = max(8, int(SR * grain_ms / 1000))
    envelope = np.exp(-np.linspace(0, 6, grain_len))
    position = 0.0
    while position < total:
        index = int(position)
        rate = max(1.0, density[min(index, total - 1)])
        amplitude = 0.35 + 0.65 * generator.random() ** 1.7
        centre = band[0] * (band[1] / band[0]) ** generator.random()
        grain = generator.standard_normal(grain_len) * envelope
        out[index:index + grain_len] += bandpass(grain, centre, 2.2) * amplitude
        position += SR / rate * (1.0 + jitter * (generator.random() - 0.5))
    return out[:total]


def place(target: np.ndarray, sound: np.ndarray, at_seconds: float,
          gain: float = 1.0) -> np.ndarray:
    """Mix `sound` into `target` at an offset, growing the buffer when needed."""
    start = max(0, int(SR * at_seconds))
    end = start + sound.size
    if end > target.size:
        target = np.pad(target, (0, end - target.size))
    target[start:end] += sound * gain
    return target


# ------------------------------------------------------ loudness / output
def _k_weight(x: np.ndarray) -> np.ndarray:
    stage1 = biquad(x, *_rbj("highshelf", 1681.97, 0.7071, 3.999))
    return biquad(stage1, *_rbj("highpass", 38.135, 0.5))


def integrated_lufs(x: np.ndarray) -> float:
    """BS.1770-4 integrated loudness with the absolute and -10 LU relative gates."""
    weighted = _k_weight(x.astype(np.float64))
    block = int(SR * 0.4)
    hop = max(1, block // 4)
    if weighted.size < block:
        weighted = np.pad(weighted, (0, block - weighted.size))
    powers = np.array([
        float(np.mean(weighted[start:start + block] ** 2))
        for start in range(0, weighted.size - block + 1, hop)
    ])
    loudness = -0.691 + 10 * np.log10(np.maximum(powers, 1e-12))
    kept = powers[loudness > -70.0]
    if not kept.size:
        return -70.0
    relative = -0.691 + 10 * np.log10(float(np.mean(kept))) - 10.0
    kept = powers[(loudness > -70.0) & (loudness > relative)]
    if not kept.size:
        return -70.0
    return float(-0.691 + 10 * np.log10(float(np.mean(kept))))


def momentary_max_lufs(x: np.ndarray) -> float:
    """Loudest 400 ms window, ungated (BS.1770 momentary, max-held).

    Integrated loudness is the wrong ruler for foley: a 50 ms click and a 1.4 s
    swell are both mostly silence inside a 400 ms block, so gating throws the
    short one away and normalising to it would slam the peak. Momentary-max
    measures what the ear actually gets hit with, and it behaves the same for a
    click, a paper slide and a swell - which is exactly what makes one shared
    target meaningful across the whole library.
    """
    weighted = _k_weight(x.astype(np.float64))
    block = int(SR * 0.4)
    if weighted.size < block:
        weighted = np.pad(weighted, (0, block - weighted.size))
    hop = max(1, int(SR * 0.05))
    powers = [
        float(np.mean(weighted[start:start + block] ** 2))
        for start in range(0, weighted.size - block + 1, hop)
    ]
    return float(-0.691 + 10 * np.log10(max(max(powers), 1e-12)))


def true_peak_db(x: np.ndarray) -> float:
    """4x oversampled peak - catches inter-sample peaks the raw max misses."""
    if x.size < 2:
        return -120.0
    upsampled = np.interp(
        np.linspace(0, x.size - 1, x.size * 4),
        np.arange(x.size), x,
    )
    return 20 * math.log10(max(float(np.max(np.abs(upsampled))), 1e-9))


def calibrate(x: np.ndarray, target_lufs: float = TARGET_LUFS,
              ceiling_db: float = TRUE_PEAK_CEILING,
              speech_safe: bool = True) -> np.ndarray:
    """Shape, loudness-match and peak-limit one sound so packs stay comparable.

    Gain is applied linearly, never soft-clipped: a limiter that reshapes a
    transient is exactly what makes cheap foley sound cheap.
    """
    x = np.asarray(x, dtype=np.float64)
    if not np.any(x):
        return x
    x = x - float(np.mean(x))
    if speech_safe:
        # Leave the 300 Hz - 3.5 kHz speech band some room; foley reads from its
        # transient and its air, not from the middle of the voice.
        x = peaking(x, 1100.0, 0.9, -3.0)
        x = highpass(x, 55.0)
    x = x * 10 ** ((target_lufs - momentary_max_lufs(x)) / 20)
    headroom = ceiling_db - true_peak_db(x)
    if headroom < 0:
        x = x * 10 ** (headroom / 20)
    return x


def trim_silence(x: np.ndarray, floor_db: float = -60.0, pad_ms: float = 6.0) -> np.ndarray:
    """Drop leading/trailing digital silence so `offset` values mean what they say."""
    if not np.any(x):
        return x
    threshold = float(np.max(np.abs(x))) * 10 ** (floor_db / 20)
    loud = np.flatnonzero(np.abs(x) > threshold)
    if not loud.size:
        return x
    pad = int(SR * pad_ms / 1000)
    return x[max(0, loud[0] - pad):min(x.size, loud[-1] + pad)]


def fade(x: np.ndarray, in_ms: float = 3.0, out_ms: float = 18.0) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64).copy()
    n_in, n_out = int(SR * in_ms / 1000), int(SR * out_ms / 1000)
    if 0 < n_in < x.size:
        x[:n_in] *= np.linspace(0, 1, n_in) ** 0.6
    if 0 < n_out < x.size:
        x[-n_out:] *= np.linspace(1, 0, n_out) ** 1.4
    return x


def write_wav(path: Path, x: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(pcm.tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        channels, width, rate = handle.getnchannels(), handle.getsampwidth(), handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: only 16-bit PCM sources are supported")
    data = np.frombuffer(frames, dtype="<i2").astype(np.float64) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != SR:
        data = np.interp(
            np.linspace(0, data.size - 1, int(data.size * SR / rate)),
            np.arange(data.size), data,
        )
    return data
