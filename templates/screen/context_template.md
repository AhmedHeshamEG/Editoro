# Template Pack Context — Screen

## Identity
- **id:** `screen`
- **display name:** سكرين · Screen
- **category color:** `#B388FF`
- **directive verb:** `سكرين`

## What it is
A screenshot of an article, post, or interface sits inside a restrained handmade browser frame with violet tape. It communicates “this came from a screen” without looking corporate.

## Fields
| field | type | notes |
|---|---|---|
| asset | asset:image | screenshot image |

## Placement defaults
| orientation | zone | position | scale |
|---|---|---|---|
| horizontal | side | `0.68, 0.40` | `1.00` |
| vertical | upper | `0.50, 0.32` | `0.90` |

## Animation
- **entrance:** 300 ms subtle scale/settle
- **hold:** static
- **exit:** 220 ms fade
- **default duration:** 4.5 s

## SFX
| file | fires at | volume |
|---|---|---|
| `ui-settle.wav` | entrance start | `0.18` |

## Assets
- `render.js` — deterministic handmade browser-frame renderer shared by preview and export
- `frame.png` — original generated transparent paper browser frame
- `ui-settle.wav` — subtle modern UI select from Mixkit; see `../SFX_SOURCES.md`

## Rules
- Screenshot text must remain legible.
- The frame never invents site branding.
- Default placement avoids the face.

## Out of scope
- Live web browsing, scrolling capture, and webpage interaction.
