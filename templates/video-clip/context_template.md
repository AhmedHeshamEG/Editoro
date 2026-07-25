# Template Pack Context — Video Clip

## Identity
- **id:** `video-clip`
- **display name:** ب-رول · Video clip
- **category color:** `#6C9BFF`
- **directive verb:** `ب-رول`

## What it is
A moving B-roll clip appears inside the same taped instant-photo frame as Image Pop. Preview and export seek the clip against timeline time; its original audio may be mixed at an instance-specific gain.

## Fields
| field | type | notes |
|---|---|---|
| asset | asset:video | project video asset |
| volume | number | clip-audio gain from `0.0` to `1.0`; default `0.8` |

## Placement defaults
| orientation | zone | position | scale |
|---|---|---|---|
| horizontal | side | `0.72, 0.35` | `1.00` |
| vertical | upper | `0.50, 0.28` | `0.90` |

## Animation
- **entrance:** 420 ms drop/settle
- **hold:** live clip playback
- **exit:** 260 ms shrink/fade
- **default duration:** 6.0 s

## SFX
| file | fires at | volume |
|---|---|---|
| `paper-land.wav` | 30 ms after entrance | `0.22` |

## Assets
- `render.js` — deterministic, timeline-synchronized clip renderer shared by preview and export
- `frame.png` — original generated transparent taped paper frame
- `paper-texture.png` — self-contained ruled-paper surface
- `paper-land.wav` — short newspaper landing from Mixkit; see `../SFX_SOURCES.md`

## Rules
- Clip playback begins at its own zero time when the instance begins.
- Audio is trimmed to instance duration and never extends the export.
- Preserve aspect ratio and avoid the face.

## Out of scope
- Clip-internal cutting beyond the instance duration and start-at-zero policy.
