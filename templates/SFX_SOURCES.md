# Template foley

The included non-camera sounds come from Mixkit's free sound-effect library. Mixkit permits use in personal and commercial video projects without required attribution. The files are trimmed where needed, faded, converted to 48 kHz mono PCM, and normalized conservatively so speech remains dominant. See the [Mixkit license](https://mixkit.co/license/#sfxFree).

| Template | Included file | Recording | Processing |
|---|---|---|---|
| Highlight | `highlight/dry-stroke.wav` | [Writing pencil](https://mixkit.co/free-sound-effects/paper/) (item 3194) | Dry 750 ms writing stroke |
| Keyword | `keyword/tactile-click.wav` | [Typewriter soft click](https://mixkit.co/free-sound-effects/click/) (item 1125) | Complete 219 ms tactile click |
| Image pop | `image-pop/paper-slide.wav` | [Paper slide](https://mixkit.co/free-sound-effects/paper/) (item 1530) | Clean 760 ms paper movement |
| Video clip | `video-clip/paper-land.wav` | [Newspaper falling to the floor](https://mixkit.co/free-sound-effects/paper/) (item 386) | Complete 614 ms landing |
| Screen | `screen/ui-settle.wav` | [Modern technology select](https://mixkit.co/free-sound-effects/click/) (item 3124) | Complete 500 ms UI settle |
| Punch-in | none | Camera motion is intentionally silent. | — |
| Zoom out | none | Camera motion is intentionally silent. | — |

Each template applies an additional speech-safe gain in its `template.json`. Highlight repeats the marker recording once per drawn stroke and spaces those events across the sweep animation.
