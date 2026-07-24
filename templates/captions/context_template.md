# Template Pack Context — Captions

## Identity
- **id:** `captions`
- **display name:** Captions
- **category color:** `#F2F2F2`
- **directive verb:** none

## What it is
Readable on-video captions with Rubik type, a quiet translucent dark backing, and direct timeline editing. The typography already includes the matching Arabic subset for a future Arabic-caption release.

## Fields
Captions use imported `text`, `start`, `end`, and optional word timestamps rather than template-instance fields.

## Placement defaults
| orientation | zone | position | scale |
|---|---|---|---|
| horizontal | lower_third | `0.50, 0.88` | `1.00` |
| vertical | lower_third | `0.50, 0.80` | `1.00` |

## Animation
Static per block. Timing comes directly from the caption track.

## Assets
- `render.js` — deterministic two-line safe-area caption renderer shared by preview and export
- `Inter-Latin-Variable.woff2` — editor interface font
- `Rubik-Latin-Variable.woff2` — English caption font
- `Rubik-Arabic-Variable.woff2` — matching future Arabic subset
- `paper-texture.png` — shared original paper texture for future style variants

## Rules
- English editing is enabled in v1; Arabic glyph support is bundled but Arabic caption workflow remains disabled.
- Maintain high contrast and never place captions outside the safe lower-third area.
- Caption text remains fully user-editable.

## Out of scope
- Transcription, translation, karaoke effects, and Arabic caption segmentation.
