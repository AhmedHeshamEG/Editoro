# Template Pack Context — Highlight

## Identity
- **id:** `highlight`
- **display name:** Highlight
- **category color:** `#F9F871`
- **directive verb:** none

## What it is
An image or PDF page appears as paper and receives one or more straight horizontal fluorescent-marker sweeps. Each drag creates one ruler-locked stroke for a line of text.

## Fields
| field | type | notes |
|---|---|---|
| asset | asset:image | image or PDF; PDFs are rasterized per page |
| page | number | zero-based PDF page index; ignored for images |
| strokes | strokes | normalized `{x1,x2,y}` rows, locked to 0° |

## Placement defaults
| orientation | zone | position | scale |
|---|---|---|---|
| horizontal | fullscreen | `0.50, 0.45` | `1.00` |
| vertical | fullscreen | `0.50, 0.40` | `0.90` |

## Animation
- **entrance:** 260 ms page fade
- **hold:** left-to-right staggered sweep per stroke
- **exit:** 220 ms fade
- **default duration:** 5.0 s

## SFX
| file | fires at | volume |
|---|---|---|
| `marker-sweep.wav` | once per drawn stroke, staggered with the ink animation | `0.22` |

## Assets
- `render.js` — deterministic page and staggered marker-sweep renderer shared by preview and export
- `stroke.png` — original generated transparent fluorescent marker stroke
- `marker-sweep.wav` — trimmed CC0 recording of a real marker on paper; see `../SFX_SOURCES.md`

## Rules
- Strokes remain exactly horizontal.
- One gesture equals one stroke; multiple lines require multiple gestures.
- The highlighted page remains readable beneath translucent ink.

## Out of scope
- Freehand drawing, angled strokes, OCR, or PDF editing.
