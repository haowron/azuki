# Bundled JavaScript

The catalog view and the run flow view lazy-load the following pinned assets (the run flow view uses only Mermaid). No CDN
connection is needed at run time. Each file is placed after verifying the `dist.integrity` (SHA-512) of the published npm tarball.

| Library | Version | Distributed file | Purpose |
|---|---|---|---|
| [Mermaid](https://github.com/mermaid-js/mermaid) | 11.12.2 | `mermaid/dist/mermaid.min.js` → `mermaid.min.js` | Diagrams of runs, delegation, and tool use; flowcharts and Gantt charts of runs and sessions; edge IDs; edge animations showing direction |
| [Anime.js](https://github.com/juliangarnier/anime) | 4.5.0 | `animejs/dist/bundles/anime.umd.min.js` → `anime.umd.min.js` | Light dots moving along SVG paths; play, pause, speed, and disposal on redraw |

SHA-256:

- `mermaid.min.js`: `d0830a6c05546e9edb8fe20a8f545f3e0dc7c4c3134d584bad9c13a99d7a71e0`
- `anime.umd.min.js`: `8d5b3a58a1f64023a04a4cedeef135a2263b18da04b35177ac13438a0bad033b`

The license texts are bundled as `mermaid.LICENSE` and `animejs.LICENSE`.
To update, fetch the distributed files and licenses from the corresponding npm version, then update this table and the hashes.
Check the diagrams, themes, playback controls, and narrow-width layout.
