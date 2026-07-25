# Zoom out

## Purpose

Start on a selected close-up of the speaker footage, then ease smoothly back to the complete source frame. Use it at a shot or source-cut boundary.

## Fields

| field | type | behavior |
|---|---|---|
| `rect` | rect | normalized starting close-up selected directly on the preview |

## Placement

Both orientations use the full source frame at `0.50, 0.50`, scale `1.00`.

## Animation

- **entrance:** 650 ms cubic zoom-out from the selected close-up
- **hold:** stable full source frame
- **exit:** remains at the full source frame

## SFX

None. Camera zooms are intentionally silent.

## Assets

- `render.js` — shared camera-transform definition used by preview and export

## Acceptance

- The selected close-up is centered correctly at the first frame.
- The move eases to exactly 1× without overshoot.
- Preview and export use identical geometry and timing.
- No SFX event is created.
