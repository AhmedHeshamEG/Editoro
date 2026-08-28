"""Build Editoro's shared texture library.

Run:  .venv\\Scripts\\python.exe tools/gen_art.py

Every texture is generated procedurally and seeded, so the repository carries
small, reproducible PNGs instead of stock art with unclear licensing. PNGs are
written directly with zlib so no imaging dependency is needed.

Output: templates/_shared/art/*.png
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parent.parent
ART = ROOT / "templates" / "_shared" / "art"


# ------------------------------------------------------------------- PNG io
def write_png(path: Path, rgba: np.ndarray) -> None:
    """Write an (h, w, 4) uint8 array as a PNG. No imaging library required."""
    height, width, channels = rgba.shape
    assert channels == 4, "expected RGBA"
    raw = b"".join(
        b"\x00" + rgba[row].tobytes() for row in range(height)
    )

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------- noise
def _lattice(size: int, cells: int, seed: int) -> np.ndarray:
    """One octave of wrapped value noise -- wrapping is what makes it tile."""
    generator = np.random.default_rng(seed)
    grid = generator.random((cells, cells))
    grid = np.pad(grid, ((0, 1), (0, 1)), mode="wrap")
    coords = np.linspace(0, cells, size, endpoint=False)
    x0 = np.floor(coords).astype(int)
    frac = coords - x0
    smooth = frac * frac * (3 - 2 * frac)          # smoothstep, C1 continuous
    x1 = (x0 + 1) % (cells + 1)
    top = grid[np.ix_(x0, x0)] * (1 - smooth)[None, :] + grid[np.ix_(x0, x1)] * smooth[None, :]
    bottom = grid[np.ix_(x1, x0)] * (1 - smooth)[None, :] + grid[np.ix_(x1, x1)] * smooth[None, :]
    return top * (1 - smooth)[:, None] + bottom * smooth[:, None]


def fbm(size: int, seed: int, octaves: int = 5, base_cells: int = 4,
        gain: float = 0.5) -> np.ndarray:
    """Seamless fractal noise in 0..1."""
    total = np.zeros((size, size))
    amplitude, weight = 1.0, 0.0
    for octave in range(octaves):
        cells = base_cells * 2 ** octave
        if cells > size:
            break
        total += amplitude * _lattice(size, cells, seed + octave * 977)
        weight += amplitude
        amplitude *= gain
    total /= max(weight, 1e-6)
    return (total - total.min()) / max(total.max() - total.min(), 1e-6)


def fibres(size: int, seed: int, strength: float = 0.5) -> np.ndarray:
    """Directional streaks: real paper has a grain direction, flat noise doesn't."""
    generator = np.random.default_rng(seed)
    field = generator.random((size, size))
    kernel = np.hanning(24)
    kernel /= kernel.sum()
    smeared = np.apply_along_axis(
        lambda row: np.convolve(np.concatenate([row[-24:], row, row[:24]]), kernel, "same")[24:-24],
        axis=1, arr=field,
    )
    smeared = (smeared - smeared.min()) / max(np.ptp(smeared), 1e-6)
    return 0.5 + strength * (smeared - 0.5)


def to_rgba(rgb: np.ndarray, alpha: np.ndarray | float = 255.0) -> np.ndarray:
    height, width = rgb.shape[:2]
    out = np.zeros((height, width, 4), dtype=np.uint8)
    out[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    if np.isscalar(alpha):
        out[..., 3] = int(alpha)
    else:
        out[..., 3] = np.clip(alpha, 0, 255).astype(np.uint8)
    return out


# ------------------------------------------------------------- textures
def paper(size: int, seed: int, base: tuple[int, int, int],
          contrast: float = 14.0, speck: float = 0.35) -> np.ndarray:
    """Seamless sheet: fine tooth, a grain direction, and a few darker specks."""
    tooth = fbm(size, seed, octaves=6, base_cells=8)
    grain = fibres(size, seed + 41, 0.6)
    generator = np.random.default_rng(seed + 99)
    specks = (generator.random((size, size)) > (1.0 - 0.0015 * speck)).astype(float)
    specks = np.clip(np.convolve(specks.ravel(), np.ones(3) / 3, "same").reshape(size, size), 0, 1)
    shade = (tooth - 0.5) * contrast + (grain - 0.5) * contrast * 0.7 - specks * 26.0
    rgb = np.stack([
        np.clip(base[0] + shade, 0, 255),
        np.clip(base[1] + shade * 1.02, 0, 255),
        np.clip(base[2] + shade * 1.08, 0, 255),
    ], axis=-1)
    return to_rgba(rgb)


def grain_overlay(size: int, seed: int, amount: float = 22.0) -> np.ndarray:
    """Neutral film grain as an alpha-only overlay -- packs tint it themselves."""
    generator = np.random.default_rng(seed)
    field = generator.standard_normal((size, size))
    field = (field - field.min()) / max(np.ptp(field), 1e-6)
    alpha = np.abs(field - 0.5) * 2 * amount
    rgb = np.full((size, size, 3), 128.0) + (field - 0.5)[..., None] * 255.0
    return to_rgba(rgb, alpha)


def highlighter(width: int = 512, height: int = 96, seed: int = 7) -> np.ndarray:
    """A marker stroke: heavier at the edges, uneven, with a dry tail."""
    yy, xx = np.mgrid[0:height, 0:width]
    ny, nx = yy / (height - 1), xx / (width - 1)
    body = np.clip(1.0 - ((ny - 0.5) * 2.05) ** 4, 0, 1)
    edge_load = 1.0 + 0.35 * np.exp(-((ny - 0.18) ** 2) / 0.006)
    edge_load += 0.30 * np.exp(-((ny - 0.84) ** 2) / 0.006)
    texture = fbm(max(width, height), seed, octaves=5, base_cells=6)[:height, :width]
    dryness = 0.72 + 0.5 * texture
    entry = np.clip(nx / 0.04, 0, 1) ** 0.7
    exit_tail = np.clip((1 - nx) / 0.10, 0, 1) ** 0.55
    alpha = body * edge_load * dryness * entry * exit_tail * 235.0
    rgb = np.stack([
        np.full((height, width), 250.0),
        np.full((height, width), 240.0) - texture * 30,
        np.full((height, width), 96.0) + texture * 40,
    ], axis=-1)
    return to_rgba(rgb, alpha)


def tape(width: int = 320, height: int = 96, seed: int = 12) -> np.ndarray:
    """Washi tape strip with torn ends and a translucent body."""
    yy, xx = np.mgrid[0:height, 0:width]
    ny, nx = yy / (height - 1), xx / (width - 1)
    texture = fbm(max(width, height), seed, octaves=4, base_cells=10)[:height, :width]
    tear = 0.045 + 0.03 * texture[:, :1]
    inside = (nx > tear) & (nx < 1 - tear)
    band = np.clip(1.0 - ((ny - 0.5) * 2.15) ** 8, 0, 1)
    alpha = band * inside * (150 + texture * 60)
    rgb = np.stack([
        np.full((height, width), 236.0) - texture * 18,
        np.full((height, width), 226.0) - texture * 20,
        np.full((height, width), 198.0) - texture * 26,
    ], axis=-1)
    return to_rgba(rgb, alpha)


def vignette(size: int = 512) -> np.ndarray:
    """Radial darkening used by the spotlight and ambient packs."""
    yy, xx = np.mgrid[0:size, 0:size]
    radius = np.hypot(yy / (size - 1) - 0.5, xx / (size - 1) - 0.5) * 2
    alpha = np.clip((radius - 0.45) / 0.75, 0, 1) ** 1.6 * 255
    return to_rgba(np.zeros((size, size, 3)), alpha)


def ambient_field(size: int = 768, seed: int = 5) -> np.ndarray:
    """The slow background layer: warm paper with wide, soft light pooling."""
    base = fbm(size, seed, octaves=6, base_cells=3)
    pools = fbm(size, seed + 313, octaves=3, base_cells=2)
    shade = (base - 0.5) * 16 + (pools - 0.5) * 26
    rgb = np.stack([
        np.clip(238 + shade, 0, 255),
        np.clip(232 + shade * 1.05, 0, 255),
        np.clip(216 + shade * 1.15, 0, 255),
    ], axis=-1)
    return to_rgba(rgb)


TEXTURES = {
    "paper-cream.png": lambda: paper(512, 101, (247, 242, 228)),
    "paper-white.png": lambda: paper(512, 202, (252, 251, 248), contrast=9.0, speck=0.15),
    "paper-kraft.png": lambda: paper(512, 303, (206, 178, 137), contrast=18.0, speck=0.8),
    "paper-dark.png": lambda: paper(512, 404, (32, 34, 40), contrast=7.0, speck=0.2),
    "paper-note.png": lambda: paper(512, 505, (252, 228, 138), contrast=11.0, speck=0.25),
    "grain.png": lambda: grain_overlay(256, 606),
    "highlighter.png": highlighter,
    "tape.png": tape,
    "vignette.png": vignette,
    "ambient-field.png": ambient_field,
}


def main() -> int:
    ART.mkdir(parents=True, exist_ok=True)
    for name, make in TEXTURES.items():
        image = make()
        write_png(ART / name, image)
        print(f"{name:<22}{image.shape[1]:>5} x {image.shape[0]:<5} "
              f"{(ART / name).stat().st_size / 1024:>7.1f} KB")
    print(f"\n{len(TEXTURES)} textures -> {ART}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
