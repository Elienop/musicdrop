# Library Search — Design

**Status:** Brainstorm output, 2026-05-25. Awaiting spec review → implementation plan.

**Goal:** Global search across the beets library — **artists, albums, and tracks** — from a header search box, on a dedicated results page. Designed so the future **playlist** feature (select tracks → add) plugs into the track results with no rework.

**Architecture (one line):** `GET /api/search?q=` (beets-query-backed, via the adapter) returns typed, capped results for the three entity types; a header search box routes to a `/search?q=` page rendering Artists / Albums / Tracks sections, reusing existing cards + a selection-ready track row.

**Tech:** FastAPI + beets adapter + Pydantic; React/shadcn + react-router + TanStack Query + openapi-fetch (generated types).

---

## Context

- Browse is a single **artist spine** (`/` = Artists roster, `/artists/:name` = albums, `/albums/:id` = tracks). The roster (home) is unpaginated — a global search box lets you find anything without scrolling it.
- beets has a **free-text query language**: `lib.items(q)` (tracks) and `lib.albums(q)` match the term across fields. Artists aren't a beets entity (derived by grouping `albumartist`).
- **Playlists are a separate, later feature.** This slice is *designed to support* track selection but does not build it.

## Decisions (from brainstorming)

1. **Scope: GLOBAL** — artists + albums + tracks.
2. **Results UX:** a dedicated `/search?q=` **results page** (three sections), reached via a header search box.
3. **Tracks are first-class** and the playlist seam — **selection-ready rows, no selection/add UI this slice**.
4. **Server-side** search via beets query; **debounced, query in the URL**.

## Backend

**Models** (`app/models/search.py`):
- `SearchTrack { id: int, title: str, artist: str, album: str, album_id: int | None, duration_seconds: float | None }` — carries `album_id` so a track links to its album page **and** is playlist-add-ready.
- `SearchResults { artists: list[Artist], albums: list[Album], tracks: list[SearchTrack], artist_total: int, album_total: int, track_total: int }` — per-type totals power "N of M".

**Adapter** (`app/beets/library.py`): `search(lib, *, query: str, limit: int) -> SearchResults`:
- **Tracks:** `lib.items(query)` → map to `SearchTrack` (`item.id, title, artist, album, album_id, length`); cap to `limit`, report total.
- **Albums:** `lib.albums(query)` → existing `_to_album`; cap + total.
- **Artists:** filter the derived roster (`list_artists`) by a **normalized-substring** match on the name; cap + total.
- Blank query → empty results (no beets call). Pass `query` to beets as free-text (matches substrings across fields); **guard** so a malformed beets query is caught → treated as no results, never a 500.

**Endpoint** (`app/api/search.py`): `GET /api/search`, `q: Annotated[str, Query(...)]` (+ optional `limit`, default ~25). Returns `SearchResults`. Empty `q` → empty results (200). Uses `get_library`; `lib is None` → empty results. Mounted under `/api`.

## Frontend

**Header search box** (`App.tsx` shell): an input in the header beside the brand, with a search icon + accessible label. Debounced (~250 ms); typing routes to `/search?q=<term>` (the `q` in the URL drives the page; use `replace` while typing so back isn't spammed). Visible on every page (global).

**`SearchPage`** (`/search` route): reads `q` from `useSearchParams`; `useSearch(q)` (TanStack Query → typed `client.GET("/api/search", { params: { query: { q } } })`, enabled only when `q` is non-empty). Renders three sections:
- **Artists** — grid of the roster poster cards (extract a shared `ArtistCard` from `ArtistsPage` to reuse). Each → `/artists/:name`.
- **Albums** — grid of `AlbumCard` (already shared in `album-grid.tsx`). Each → `/albums/:id`.
- **Tracks** — a **list** of self-contained `TrackRow` (*title · artist · album · duration*), each linking to `/albums/:album_id`. **This list is the playlist seam.**
- Section headers with "N of M"; sections with no hits are hidden; overall "No results for {q}"; loading skeletons; error state with retry.

**Routing** (`main.tsx`): add `/search` → `SearchPage`. **Schema regen** (`gen:api`) for the new endpoint + models.

## Playlist-readiness (designed-for, NOT built)

`SearchTrack` carries `id` + `album_id` + meta — everything add-to-playlist needs. The Tracks section is a list of self-contained `TrackRow`s, so the playlist slice adds a per-row checkbox + a sticky selection/action bar **into this structure** with no rework. No selection/add UI in this slice.

## Trigger & URL

Debounced-as-you-type (~250 ms); `q` lives in the URL (`/search?q=`) → bookmarkable, shareable, back works. Typing in the header box from any page navigates to `/search`.

## Error handling

`lib is None` / blank `q` → empty results (no crash). Malformed beets query → caught → empty/partial, never 500. FE network error → error state.

## Testing

- **Backend:** matching artists/albums/tracks for a term; caps + totals; blank `q` → empty; `SearchTrack` carries `album_id`; `lib is None` → empty; no-match → empty. Hermetic beets fixture (like existing adapter tests).
- **Frontend:** `SearchPage` renders the three sections from a mocked `/api/search`; track rows link to `/albums/:album_id`; header box updates the URL + navigates; debounce; empty/loading/error states; "N of M".

## Build chunks

1. **Backend** — models + adapter `search()` + `GET /api/search` + tests (curl-able).
2. **Frontend** — header search box + `SearchPage` (3 sections, reused cards + `TrackRow`) + routing + schema regen + tests + live screenshot.

## Out of scope

- Track multi-select + add-to-playlist (the playlist feature).
- ⌘K command palette (can layer on later).
- Power query-syntax UI (free-text only; beets `field:value` may work as a bonus).
- Per-section pagination / "see all N" (cap + total now; a "see all" view later if wanted).
