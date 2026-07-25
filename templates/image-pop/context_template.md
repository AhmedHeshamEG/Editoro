# Template Pack Context — Image Pop

## Identity
- **id:** `image-pop`
- **display name:** صورة · Image pop
- **category color:** `#4ECDC4`
- **directive verb:** `صورة`

## What it is
An image drops into a handmade instant-photo paper frame with teal tape, settles naturally, and stays beside—not over—the speaker. It makes sourced imagery feel physically placed on the same desk.

## Fields
| field | type | notes |
|---|---|---|
| asset | asset:image | PNG, JPEG, WebP, GIF first frame, or rasterized PDF page |
| page | number | zero-based PDF page index; ignored for images |

## Placement defaults
| orientation | zone | position | scale |
|---|---|---|---|
| horizontal | side | `0.72, 0.35` | `1.00` |
| vertical | upper | `0.50, 0.28` | `0.90` |

## Animation
- **entrance:** 420 ms drop/overshoot/settle
- **hold:** stable with deterministic slight paper rotation
- **exit:** 260 ms shrink/fade
- **default duration:** 4.0 s

## SFX
| file | fires at | volume |
|---|---|---|
| `paper-slide.wav` | 30 ms after entrance | `0.24` |

## Assets
- `render.js` — deterministic aspect-preserving paper-frame renderer shared by preview and export
- `frame.png` — original generated transparent taped paper frame
- `paper-texture.png` — self-contained ruled-paper surface
- `paper-slide.wav` — clean paper slide from Mixkit; see `../SFX_SOURCES.md`

## Rules
- Preserve the source image aspect ratio.
- Missing imagery stays visibly dim until filled.
- Keep the default away from the speaker’s face.

## Out of scope
- Freeform borders, filters, and image retouching.
