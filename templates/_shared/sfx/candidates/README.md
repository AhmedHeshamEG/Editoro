# Counting sounds to audition

**Decided: `count-wood.wav`.** It is installed as
`templates/_shared/sfx/count-wood.wav` and `stat-pop` plays it once per notch of
the count, decelerating with the digits. This folder is kept as the record of
what was compared; nothing here is loaded by a template.


The sound the Stat template plays while a number climbs is wrong — it reads as a
UI notification, and a number counting up on a paper card should sound like a
mechanism or a pencil. Listen to these and say which one, and it goes into
`templates/stat-pop/template.json` in place of `count-tick.wav`.

**Listen to `DEMO-twelve-wood-ticks.wav` first.** Auditioning a click on its own
tells you almost nothing — a counting sound is twelve of them in a second, and
what matters is whether that reads as a mechanism or as a woodpecker.

| File | What it is |
|---|---|
| `ZZ-current-count-tick.wav` | What is in there now, for comparison. |
| `count-wood.wav` | A wooden tick — two close resonances struck once. Closest to a counter wheel. My pick. |
| `count-mechanical.wav` | A counter detent: harder, drier, almost pitchless, with a small rattle. |
| `count-pencil.wav` | Graphite on paper. No pitch at all, the quietest of the four. |
| `count-swell.wav` | Not a tick — one soft rise that lands with the number. Choose this if you would rather the count were felt than counted; it plays once per block instead of once per digit. |
| `pencil-tick.wav`, `tick.wav`, `soft-click.wav` | Already in the library, copied here so everything is in one folder. |

All four `count-*` files are synthesised, not sourced, so there is no licence on
any of them and nothing to credit. They are 48 kHz mono PCM at the same
conservative level as the rest of the library, so whichever you pick drops
straight in. `tools/make_count_sfx.py` regenerates them and is where the
synthesis is explained.

Picking `count-swell.wav` is a slightly different change from picking a tick: it
needs its own `sfx` entry with no `repeat_spacing`, because it is one sound
across the whole count rather than one per digit.
