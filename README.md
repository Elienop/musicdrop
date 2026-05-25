# MusicDrop

**The eye on your music library** — see what you have (and what you're missing), and drive your tagging and enrichment without ever touching a terminal.

MusicDrop is a from-scratch rebuild. It keeps the name, logo, and product vision of the original (now archived at `../MusicDrop-old`), but inverts the architecture: rather than maintaining its own metadata matcher, MusicDrop puts a comprehensive **web UI on top of [beets](https://beets.io)** — the mature, ~15-year-old music tagger — and adds what beets lacks: a rich visual library, playlists, and multi-user sync.

## What it does

- **See your library.** Browse artists, albums, and tracks; spot the gaps (what you have vs. don't); search, filter, and view cover art, lyrics, and track info. The library as a visual surface, not a file explorer.
- **A UI for beets.** beets' *toggles* (its config) and *actions* (`import`, `modify`, `fetchart`, `duplicates`, …) surfaced as real pages — do everything you'd do at the `beet` CLI, in the browser. The interactive import/match step gets a proper candidate-picker.
- **Playlists.** Create, edit, and delete them easily. **Plex-compatible**, multi-user.
- **Acquisition.** Integrates deemix / slskd to get music onto disk (beets has no acquisition layer), then hands files to beets to match and organize.

## Architecture — the layers

beets owns the **engine and the library** (its `library.db` is the source of truth for matching, tagging, organizing). MusicDrop is the **web face and value-add** around it:

| Layer | Owner |
|---|---|
| Acquisition (deemix / slskd) | MusicDrop |
| Match · enrich · organize · library | **beets** |
| Decision (auto-accept policy + human review UI) | MusicDrop |
| Presentation (browse · search · art / lyrics / info) | MusicDrop |
| Playlists · Plex sync · multi-user | MusicDrop |

beets and MusicDrop are co-located on the same host: beets' library (`library.db`) and config (`config.yaml`) live on a shared path; beets' actions run via its CLI/Python (and, where it helps, a small long-lived beets service).

## Status

🚧 **Greenfield, day one.** The prior Go implementation is archived at `../MusicDrop-old` for reference — the React frontend, the deemix/slskd integration, and the UX patterns will be ported from it.

## Open decisions

- **Backend language.** Go (with beets as a Python *sidecar* over a local port) vs. **Python-native** (FastAPI/Flask wrapping beets directly — collapses to a single runtime, since deemix is Python and slskd is just an HTTP API). *Currently leaning Python-native; not yet locked.*
