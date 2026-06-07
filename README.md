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

beets and MusicDrop are co-located on the same host: beets' library (`library.db`) and config (`config.yaml`) live on a shared path; beets' actions run in-process through a typed adapter (`app/beets/`).

## Status

**Actively built.** A deep beets integration and the web UI are in place. The React frontend and UX patterns are ported from the prior Go implementation, archived at `../MusicDrop-old`.

**Shipped**

- **Browse** — artist → albums → tracklist, with cover art, lyrics, and a release's missing tracks.
- **Search** across the library.
- **Cover art** — fetch + replace. **Artist images** — multi-source (fanart.tv / Spotify / Deezer) with manual override, written into the library for Plex.
- **Lyrics** — presence, per-album fetch, and a library-wide backfill.
- **Edit tags** — album & track, from the UI.
- **Import** — interactive candidate picker, resume, and duplicate handling.
- **beets config** — viewer + writable editor.
- **Naming** — edit beets path/replace rules with a live preview. **Reorganize** — re-apply them to existing files.
- **Library dashboard** — counts, duration, size, recently added.
- **Playlists** — create / edit / delete; `.m3u8` export; Plex-compatible, multi-user sync (metadata-matched, with cascade-delete).

**Planned** — acquisition (deemix / slskd), faceted search.

## Decisions

- **Backend: Python-native** (FastAPI + Pydantic, beets driven in-process behind a typed adapter in `app/beets/`). Locked — it collapses to a single runtime, since deemix is Python and slskd is just an HTTP API.
