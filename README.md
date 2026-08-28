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
- **Cover art** — fetch + replace.
- **Artist images** — portraits resolve automatically from the configured sources (fanart.tv → Spotify → Deezer, first verified match wins; Deezer needs no key) and are written into the library for Plex. To change one, open an artist and use the image action: pick a source, **Fetch**, and **Use this image** to keep it — or upload a file / paste a URL. **Reset to auto** forgets both your pick and the cached automatic image, so the artist is looked up again from scratch. An artist whose portrait isn't cached yet shows their initials while it resolves in the background; it appears without a reload when it lands.
- **Lyrics** — presence, per-album fetch, and a library-wide backfill. The backfill is
  **fill-gaps-only on disk**: it writes a `.lrc`/`.txt` sidecar only where none exists and
  never deletes or replaces one you already have (the sole removal is a file whose entire
  content is the legacy `[Instrumental]` marker, cleaned when a track is classified
  instrumental).
- **Edit tags** — album & track, from the UI.
- **Import** — interactive candidate picker, resume, an import-time duplicate guard, and search-by-release-ID when the right match isn't offered. Unattended runs **bank** undecidable albums for later review instead of stalling, and the summary verifies each album actually **landed** in the library.
- **Duplicates** — find & resolve duplicate albums (resolve one, or resolve-all).
- **Release identity** — which release an album is (source · label · country · media · disambiguation), with view-release links.
- **Delete & Trash** — delete albums or artists into a reversible Trash; restore or empty it
  under **Settings → Trash**. If the music root is missing, empty or unreadable (an unmounted
  share), deletes are refused with a 503 — nothing is moved and no library rows are dropped —
  so a genuinely emptied library needs a remount (or beets' own CLI) before its leftover
  entries can be cleared. A share dropping part-way through an artist delete reports how
  many albums were trashed before it dropped — those stay recoverable in Trash, the rest
  untouched. A Trash row holding no audio files (art/booklet folders swept as
  leftovers) can't be restored by re-import, so its Restore button is disabled with the
  reason — Empty is its only exit.
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
    container_name: musicdrop   # the `docker inspect musicdrop` commands below assume this
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

**Browsing by DNS name?** Requests are only accepted when the `Host` is an IP literal,
`localhost`, or a name listed in `MUSICDROP_ALLOWED_HOSTS` (comma-separated) — a
DNS-rebinding guard, same shape as Plex's and Transmission's. Reaching MusicDrop through a
reverse proxy or any hostname (`http://nas.local:3030`, `https://music.example.com`) requires
listing that name: `MUSICDROP_ALLOWED_HOSTS=music.example.com`. By-IP access always works.
Behind a proxy that rewrites `Host`, the forwarded public name (`X-Forwarded-Host`) must be in the list too — Caddy forwards both by default.
The same goes for server-to-server callers — a proxy that rewrites `Host` to an upstream
*name*, or another container calling MusicDrop by service name (slskd's webhook posting to
`http://musicdrop:3030`) — list those names too; container/host IPs always work.

Releases are automatic: every merged PR publishes a new image tag (`vX.Y.Z`, plus `latest`) with generated notes on the [Releases page](https://github.com/Elienop/musicdrop/releases).

**One-time repair, for libraries built before the path fix.** Every release up to and including v0.34.0 stored MusicDrop-imported track paths in `library.db` *absolutely* (`/music/Artist/…`) instead of relative to the music directory, the way beets does. Nothing is damaged, but such rows do not follow the music share if it ever moves to a new mount, dataset, or machine. The release containing `fix(import): store item paths relative to the music dir` stops it recurring; the existing rows need a separate one-time database repair, and **the two have to land in the same maintenance window** — deploying either half on its own leaves the importer's duplicate lookup worse off than doing neither. The procedure, starting with the census that tells you whether you are affected, is [`docs/import-path-repair.md`](docs/import-path-repair.md).

## Backup & restore

MusicDrop has no built-in backup, deliberately: its state is plain files under the paths you already mount, so a **filesystem snapshot** (ZFS/btrfs on the NAS) is the supported mechanism — nothing to export, and restoring is putting the files back.

**What to snapshot**

| Host path | Mount | Holds |
|---|---|---|
| `./data` | `/data` | `beets/` — library DB, config, bank, playlists, settings, Trash — plus `cache/artist-images/` and `cache/cover-thumbs/` |
| your music share | `/music` | the audio files, their embedded tags, `cover.<ext>`, `.lrc`/`.txt` lyric sidecars, `artist-poster.*` / `artist-background.*` |
| slskd downloads *(acquisition only)* | `/inbox` | `.musicdrop-ledger.json` — which drops were already handled; without it, old downloads re-import |

`/music` is the library; `/data` is every decision you have made about it. Snapshot both; the host paths above are `docker-compose.yml`'s placeholders.

**Authoritative** — losing it loses work, and nothing regenerates it:

- `data/beets/library.db` — the beets library: every match, tag and organize decision, plus the `lyrics_checked` and `lyrics_instrumental` flags. A `library.db-before-*.bak` sibling is a beets pre-migration copy, the only way back to the previous schema; having none is normal.
- `data/beets/config.yaml` — **Settings → Beets** and **Settings → Naming** both write this file in place and keep no previous copy.
- `data/beets/bank/*.json` — albums banked for review. Pending decisions, not a cache.
- `data/beets/playlists/*.json` and `data/beets/playlists/artwork/` — MusicDrop owns playlists; Plex is a push target, not a copy.
- `<music>/.playlists/*.m3u8` — the Plex-readable exports. Rewritten only when a playlist changes, never rebuilt wholesale, so the music tree's restore is what covers them; `MUSICDROP_PLAYLISTS_EXPORT_DIR` takes them out of it — snapshot that path too.
- `data/beets/plex/plex.json`, `data/beets/slskd/slskd.json` — the Plex and slskd integration settings, mode `0600`. Not just tokens: Plex's library path/section, slskd's downloads prefix and its `auto_import` toggle (lose that and unattended import reverts to its env default, off).
- `<inbox>/.musicdrop-ledger.json` — the handled-drops record. Defaults to `<beets_dir>/inbox`, inside `/data`; `MUSICDROP_INBOX_DIR` moves it onto the slskd downloads mount — the table's third row.
- `data/beets/trash/` — deleted albums live here and nowhere else until you empty the Trash; normally the only GB-scale item under `data/`.
- `data/beets/state.pickle` — beets' import state. The banking sweep's forced `incremental` reads its `taghistory`; without it the next sweep re-offers every folder it has already handled.
- `data/cache/artist-images/` — the `*.override` (+ `*.override.mime`) images you uploaded or pasted by hand, which nothing refetches, and `_enabled.json` / `_art_write_enabled.json`, the two artist-image toggles: lose those and both revert to their env defaults (off).

Those are the shipped image's paths (`MUSICDROP_BEETS_DIR=/data/beets`, `MUSICDROP_ARTIST_IMAGE_CACHE_DIR=/data/cache/artist-images`). Override either and the tree under it moves; a `MUSICDROP_*_DIR` for trash, bank, playlists, Plex, slskd or the inbox moves that subtree out from under `<beets_dir>`; and `config.yaml`'s `library:` and `directory:` relocate the DB and the music tree with no env var at all. Snapshot what these resolve to, not the defaults.

**Regenerable** — don't worry about these:

- `data/cache/artist-images/*.bin` · `*.mime` · `*.miss` — the auto-fetch cache and its negative markers, refetched on demand.
- `data/cache/artist-images/*.thumb.bin` · `*.thumb.src` — the 320px WebP portraits the grids and rosters render, re-derived from the `*.override` or `*.bin` beside them the moment the sidecar's source tag stops matching.
- `data/cache/cover-thumbs/` — the same derivation for album covers (`MUSICDROP_COVER_THUMB_CACHE_DIR=/data/cache/cover-thumbs` in the shipped image). Nothing authoritative is here at all: unlike the artist cache this one never owns an original — every cover it thumbnails lives in the music tree, as an art file or an embedded tag. Delete the whole directory and the next page view rebuilds what it needs.
- import, backfill and sweep jobs — in memory only; they don't survive a restart anyway.

**Snapshot consistency**

You need not stop MusicDrop to take a snapshot. `library.db` is SQLite in its default rollback-journal mode (`journal_mode=delete`; neither beets nor MusicDrop switches it to WAL), so a `library.db-journal` sidecar exists only while a write transaction is open, and a snapshot atomic within the dataset captures the DB and that journal together — what SQLite needs to roll the interrupted transaction back. A snapshot of a running MusicDrop is crash-consistent; at worst one in-flight write is discarded.

Separate datasets don't change that, so long as ONE snapshot operation covers both, and [`zfs-snapshot(8)`](https://openzfs.github.io/openzfs-docs/man/master/8/zfs-snapshot.8.html) promises a shared instant for exactly one form — `-r`: "[r]ecursive snapshots created through the `-r` option are all created at the same time". Take it over a common ancestor; within a pool one always exists (`zfs list` shows your layout), and on TrueNAS it is one Periodic Snapshot Task with **Recursive** ticked. Two *separate* operations — different pools, or a task each — are two instants, and moves fall through the gap: deleting to Trash moves a whole album folder from `/music` into `data/beets/trash/` under `/data`, and inbox drops import with `operation="move"`. Caught between the instants, that album is in both snapshots, in neither, or split across them — and `shutil.move` across filesystems is copy-then-delete, so a file can be captured truncated. The result is a folder to re-import or re-delete, not a damaged library; on that layout, snapshot with the container stopped, or at least never during a delete, a Trash restore, or an inbox import.

Quiescence comes from the process being gone, not from the shutdown grace: MusicDrop waits ~5s for an in-flight import to release the slot, but the beets worker is a daemon thread it cannot join, so past that bound the library closes under a still-running import. For the snapshot you keep as the restore point of record, snapshot after `docker compose down` returns.

**Record the image tag in the snapshot's name.** Nothing inside the snapshot records it, and by restore time the container that could tell you is gone — so read it now, from the sidebar's health row, `GET /api/health`, or `docker inspect musicdrop --format '{{range .Config.Env}}{{println .}}{{end}}' | grep MUSICDROP_VERSION`.

**Never run without the `./data` bind mount**

The image's `VOLUME /data` makes a *missing* bind mount silent rather than fatal: Docker creates an anonymous volume under `/var/lib/docker/volumes/` and your library lives on a path no snapshot policy is aimed at. To confirm where it resolves:

```bash
docker inspect musicdrop --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

If that prints `/var/lib/docker/volumes/<hash>/_data -> /data`, your library is in an anonymous volume. Move it out: `docker compose down`, `mkdir -p ./data && sudo cp -a /var/lib/docker/volumes/<hash>/_data/. ./data/`, add `- ./data:/data` to the compose file, `docker compose up -d`, then re-run the inspect and confirm the app still shows your library. Leave the old volume alone — an orphaned volume costs disk, and it is your only second copy until the next snapshot runs.

**Restoring**

Restore through your NAS's snapshot tooling; a few principles are all this needs. Stop MusicDrop first — it holds the library DB open while it runs. Put back **only** the tree you actually lost: a snapshot laid over a tree you still have reverts everything changed in it since — on the music share every tag write and every cover, portrait and lyric file, and under `./data` every import, edit and decision. Restore files rather than `zfs rollback`, which discards all data changed since the snapshot across the whole dataset ([`zfs-rollback(8)`](https://openzfs.github.io/openzfs-docs/man/master/8/zfs-rollback.8.html)). Pin the image to the tag in the snapshot's name: beets migrates `library.db` on open, one-way, and an old snapshot booted once under a newer image cannot go back without the `library.db-before-*.bak` beets writes before migrating. Start the app only once everything is back in place.

**Restoring the music share onto a different path** — a renamed dataset, a new bind mount, a new box — needs [`docs/import-path-repair.md`](docs/import-path-repair.md) done first, on libraries that predate the path fix. Rows holding absolute paths do not follow the move, and the next `beet update` or disk sync removes them from the library: the audio files stay on disk, but their added dates, play counts, lyrics, flexible fields and album grouping do not. The repair is one migration and takes one maintenance window.

**Test the restore once**

An untested restore is a hypothesis. Do the drill once, while nothing is broken: restore a snapshot into a scratch directory and point a throwaway compose file at it — a different port, a different `container_name` (`docker-compose.yml` pins `musicdrop`, so a copy changing only ports and volumes clashes on the name), and the music share **read-only** (`- /path/to/music:/music:ro`) so the drill cannot touch it. If **Library → Overview** shows your library, the backup works. Never point a second container at the live `/data`: MusicDrop must be the only process holding the library open.

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

**API types are generated, not hand-written.** The frontend's TypeScript API types come from the backend's OpenAPI schema — never edit `src/api/schema.d.ts` by hand. After changing a Pydantic model, run both steps from the repo root (a CI guard fails if `frontend/openapi.json` drifts from the live spec):

```bash
cd backend && uv run python scripts/dump_openapi.py   # app -> frontend/openapi.json
cd ../frontend && npm run gen:api                     # frontend/openapi.json -> src/api/schema.d.ts
```

**Coverage** (from the repo root):

```bash
make coverage     # both suites with coverage on -> backend/coverage/, frontend/coverage/
```

It writes four gitignored reports for SonarQube: Cobertura XML and JUnit XML for the backend, `lcov.info` and Sonar's generic test-execution XML for the frontend. Coverage is opt-in and off every default path — plain `uv run pytest` and `npm run test` behave exactly as they always have and write nothing; run `npm run test:coverage` for the frontend on its own. (`sonar-scan`, which runs `make coverage` before each upload, is a local wrapper of the maintainer's and is not part of this repo.)

## Decisions

- **Backend: Python-native** (FastAPI + Pydantic, beets driven in-process behind a typed adapter in `app/beets/`). Locked — one runtime, since deemix is Python and slskd is just an HTTP API.
- **The API is the contract.** Every endpoint takes/returns Pydantic models; the OpenAPI schema is the source of truth, and the frontend TypeScript types are generated from it (never hand-written).
- **beets owns the engine + library.** All beets access lives behind the typed adapter in `app/beets/`, where beets' global config/plugin singletons stay isolated.
- **Browse is a single artist spine** (roster → artist → albums → tracks), not a flat album grid.
