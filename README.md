# MusicDrop

**The eye on your music library** — see what you have (and what you're missing), drive your tagging and enrichment, and build Plex-ready playlists, all without touching a terminal.

MusicDrop is a from-scratch rebuild. It keeps the name, logo, and product vision of the original (now archived for reference), but inverts the architecture: rather than maintaining its own metadata matcher, MusicDrop puts a comprehensive **web UI on top of [beets](https://beets.io)** — the mature, ~15-year-old music tagger — and adds what beets lacks: a rich visual library, playlists, and multi-user Plex sync.

## What it does

- **See your library.** Browse artists, albums, and tracks; spot the gaps (what you have vs. don't); search, and view cover art, lyrics, and track info. The library as a visual surface, not a file explorer.
- **A UI for beets.** beets' *toggles* (its config) and *actions* (`import`, `modify`, `fetchart`, `duplicates`, …) surfaced as real pages — do everything you'd do at the `beet` CLI, in the browser. The interactive import/match step gets a proper candidate-picker.
- **Playlists.** Create, edit, and delete them easily. **Plex-compatible**, multi-user.
- **Acquisition.** slskd downloads land in a watched inbox and flow through a unified **Review** page into the same beets import pipeline (deemix is a future adapter on the same seam).

## Architecture — the layers

beets owns the **engine and the library** (its `library.db` is the source of truth for matching, tagging, organizing). MusicDrop is the **web face and value-add** around it:

| Layer | Owner |
|---|---|
| Acquisition (slskd; deemix planned) | MusicDrop |
| Match · enrich · organize · library | **beets** |
| Decision (auto-accept policy + human review UI) | MusicDrop |
| Presentation (browse · search · art / lyrics / info) | MusicDrop |
| Playlists · Plex sync · multi-user | MusicDrop |

beets and MusicDrop are co-located on the same host: beets' library (`library.db`) and config (`config.yaml`) live on a shared path; beets' actions run in-process through a typed adapter (`app/beets/`).

## Stack

- **Backend** — Python ≥ 3.11, **FastAPI + Pydantic** on Uvicorn; **beets 2.12** runs in-process behind the typed adapter in `app/beets/`. HTTP via httpx, Plex via [python-plexapi](https://github.com/pkkid/python-plexapi), YAML config editing via ruamel.yaml. Packaged with **uv**; `mypy --strict`, **Ruff** (lint + format), and **pytest** enforced in CI.
- **Frontend** — **React 19 + TypeScript**, built with **Vite**. UI is **shadcn/ui** (Radix primitives) + **Tailwind CSS 4** + [Phosphor](https://phosphoricons.com/) icons, with **League Spartan** as the display face; server state via **TanStack Query**; routing via React Router. The API client is **openapi-fetch**, and the TypeScript API types are **generated** from the backend's OpenAPI schema — never hand-written. Tested with Vitest + Testing Library + MSW.

## Status

**Actively built.** A deep beets integration and the web UI are in place.

**Shipped**

- **Browse** — artist → albums → tracklist, with cover art, lyrics, and a release's missing tracks.
- **Search** across the library.
- **Cover art** — fetch + replace. **Artist images** — multi-source (fanart.tv / Spotify / Deezer) with per-artist override (upload or paste a URL), written into the library for Plex.
- **Lyrics** — presence, per-album fetch, and a library-wide backfill.
- **Edit tags** — album & track, from the UI.
- **Import** — interactive candidate picker, resume, an import-time duplicate guard, and search-by-release-ID when the right match isn't offered. Unattended runs **bank** undecidable albums for later review instead of stalling, and the summary verifies each album actually **landed** in the library.
- **Duplicates** — find & resolve duplicate albums (resolve one, or resolve-all).
- **Release identity** — which release an album is (source · label · country · media · disambiguation), with view-release links.
- **Delete & Trash** — delete albums or artists into a reversible Trash; restore or empty it under **Settings → Trash**.
- **beets config** — viewer + writable editor.
- **Naming** — edit beets path/replace rules with a live preview. **Reorganize** — re-apply them to existing files (and sweep emptied leftover folders into the Trash).
- **Disk sync** — a `beet update` equivalent: preview-first removal of library entries whose files were deleted outside the app, plus tag refresh for files changed on disk.
- **Library dashboard** — counts, duration, size, recently added.
- **Playlists** — create / edit / delete; `.m3u8` export; Plex-compatible, multi-user sync (metadata-matched, with cascade-delete). Configure Plex under **Settings → Plex** (base URL + admin token + the music-library path *as Plex sees it*), or seed it from `MUSICDROP_PLEX_URL` / `MUSICDROP_PLEX_TOKEN` / `MUSICDROP_PLEX_LIBRARY_PATH`.
- **Acquisition (slskd)** — completed slskd downloads land in a watched inbox (webhook-driven) and queue into the import pipeline; a unified **Review** page is the one home for import decisions and inbox backlog.
- **Faceted Browse** — slice the library by genre · decade · format · type · media · country · source · lyrics coverage, sorted A–Z or recently added.
- **Live updates** — library changes stream to every open tab (SSE), no manual refresh.
- **Dark, art-forward UI** — violet-accented dark theme, dissolving detail rails, Koito-inspired row cards, a two-font type system (League Spartan display face), and the original MusicDrop logo re-colored onto the design tokens.

**Planned** — deemix acquisition adapter.

## Screenshots

| Overview | Artist | Album |
|---|---|---|
| ![Overview — library stats and recently added](docs/screenshots/overview.png) | ![Artist page — portrait rail and album rows](docs/screenshots/artist.png) | ![Album page — cover rail and tracklist](docs/screenshots/album.png) |

## Install (Docker)

MusicDrop ships as a single container: `ghcr.io/elienop/musicdrop` (amd64), FastAPI serving both the API and the UI on port **3030**.

```yaml
services:
  musicdrop:
    image: ghcr.io/elienop/musicdrop:latest
    ports:
      - "3030:3030"
    environment:
      - PUID=1000   # match the owner of your music share
      - PGID=1000   # (tag writes / reorganize keep that ownership)
      - TZ=Etc/UTC
    volumes:
      - ./data:/data            # beets library.db + config.yaml + app state
      - /path/to/music:/music   # your music library
    restart: unless-stopped
```

`docker compose up -d`, then open `http://<host>:3030`. First boot writes a starter beets config to `data/beets/config.yaml` with `directory: /music`; edit it under **Settings → beets** (plugins, import behavior) — MusicDrop reads it like the beets CLI would. Optional integrations (slskd webhook, Plex, fanart.tv/Spotify artist images) are configured under Settings or via `MUSICDROP_*` env vars; for slskd, mount its downloads dir (e.g. `/inbox`) and set `MUSICDROP_INBOX_DIR=/inbox`.

Releases are automatic: every merged PR publishes a new image tag (`vX.Y.Z`, plus `latest`) with generated notes on the [Releases page](https://github.com/Elienop/musicdrop/releases).

## Development

MusicDrop is two apps: a FastAPI backend (`backend/`) and a Vite + React frontend (`frontend/`). Run both in dev — the frontend proxies `/api` to the backend, so there's no CORS or base-URL juggling. Layout: `backend/app/` is the API (`api/` routers · `models/` the Pydantic contract · `beets/` the beets-adapter boundary) with tests in `backend/tests/`; `frontend/` is the React + shadcn UI.

**Prerequisites:** Python ≥ 3.11 with [uv](https://docs.astral.sh/uv/); Node 22+ with npm.

**Backend** (from `backend/`):

```bash
uv sync --extra dev                                # install (incl. dev tools)
uv run uvicorn app.main:app --port 3030 --reload   # API on http://localhost:3030
uv run pytest                                      # tests
uv run mypy                                         # strict typecheck (must be clean)
uv run ruff check                                  # lint
uv run ruff format                                 # format
```

**Frontend** (from `frontend/`):

```bash
npm install        # install
npm run dev        # Vite dev server on http://localhost:5173 (proxies /api -> :3030)
npm run test       # vitest
npm run typecheck  # tsc
npm run build      # production build
```

Keep the backend on port **3030** — that's the target of the Vite dev proxy.

**API types are generated, not hand-written.** The frontend's TypeScript API types come from the backend's OpenAPI schema — never edit `src/api/schema.d.ts` by hand. After changing a Pydantic model, refresh `frontend/openapi.json` from the backend schema, then regenerate:

```bash
npm run gen:api   # frontend/openapi.json -> src/api/schema.d.ts
```

## Decisions

- **Backend: Python-native** (FastAPI + Pydantic, beets driven in-process behind a typed adapter in `app/beets/`). Locked — one runtime, since deemix is Python and slskd is just an HTTP API.
- **The API is the contract.** Every endpoint takes/returns Pydantic models; the OpenAPI schema is the source of truth, and the frontend TypeScript types are generated from it (never hand-written).
- **beets owns the engine + library.** All beets access lives behind the typed adapter in `app/beets/`, where beets' global config/plugin singletons stay isolated.
- **Browse is a single artist spine** (roster → artist → albums → tracks), not a flat album grid.
