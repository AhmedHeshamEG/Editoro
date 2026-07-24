# Template Pack Context — Punch In

## Identity
- **id:** `punch-in`
- **display name:** زوم · Punch-in
- **category color:** `#FF6B6B`
- **directive verb:** `زوم`

## What it is
A smooth emphasis zoom transforms the source footage itself toward a rectangle selected directly on the preview. No card is drawn; the camera move is the template.

## Fields
| field | type | notes |
|---|---|---|
| rect | rect | normalized target rectangle on source preview |

## Placement defaults
Both orientations use the full source frame at `0.50, 0.50`, scale `1.00`.

## Animation
- **entrance:** 450 ms eased zoom-in
- **hold:** stable target framing
- **exit:** 400 ms eased zoom-out
- **default duration:** 3.0 s

## SFX
| file | fires at | volume |
|---|---|---|
| `whoosh.wav` | entrance start | `0.24` |

## Assets
- `render.js` — deterministic source-transform definition used by preview; export applies the same easing and target geometry through FFmpeg
- `whoosh.wav` — CC0 bamboo-stick air swipe; see `../SFX_SOURCES.md`

## Rules
- Rotation is never introduced.
- The chosen rectangle must remain at least 5% of frame width and height.
- Zoom motion stays smooth and does not overshoot the target.

## Out of scope
- Keyframed pans, rotations, and face tracking.
