# Template Pack Context — Keyword

## Identity
- **id:** `keyword`
- **display name:** نص · Keyword
- **category color:** `#FFD166`
- **directive verb:** `نص`

## What it is
A short verbatim word or phrase appears on a warm torn notebook card above the speaker. It is the strongest “remember this” beat: tactile, fast, readable, and unmistakably part of the same student-desk visual language.

## Fields
| field | type | notes |
|---|---|---|
| text | text | Must be words actually spoken; one phrase, ideally under 32 characters |

## Placement defaults
| orientation | zone | position | scale |
|---|---|---|---|
| horizontal | فوق_الراس | `0.50, 0.14` | `1.00` |
| vertical | فوق_الراس | `0.50, 0.18` | `0.85` |

## Animation
- **entrance:** 350 ms overshoot pop with a slight paper tilt
- **hold:** stable and readable
- **exit:** 220 ms shrink/fade
- **default duration:** 2.5 s

## SFX
| file | fires at | volume |
|---|---|---|
| `tactile-click.wav` | entrance start | `0.22` |

## Assets
- `render.js` — deterministic two-line notebook-card renderer shared by preview and export
- `paper-texture.png` — original generated ruled-paper surface
- `tactile-click.wav` — short soft typewriter click from Mixkit; see `../SFX_SOURCES.md`
- `Rubik-Latin-Variable.woff2` — Latin on-video text
- `Rubik-Arabic-Variable.woff2` — future Arabic-ready companion

## Rules
- Text is verbatim speech, never editorial paraphrase.
- Maximum two short lines; never cover the face.
- No corporate card gradients, neon, or glossy UI styling.

## Out of scope
- Paragraphs, subtitles, and unsaid editorial claims.
