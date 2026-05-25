# Artist Images (Deezer) — Design

**Status:** Brainstorm output, 2026-05-25. Awaiting spec review → implementation plan.

**Goal:** Show real artist portraits on the artist roster cards and the artist-albums page header — fetched from Deezer, **matched carefully so we never show the wrong artist**, cached locally, falling back to the person glyph. Opt-in, rate-limited, pluggable, and built in small chunks.

**Architecture (one line):** A new `app/artwork/` module (outside the beets adapter) resolves an artist to a Deezer image **using strict name verification**, caches it on disk, and serves it via an endpoint the frontend points an `<img>` at (glyph on miss).

**Tech:** Python/FastAPI + httpx (async) + respx (test mocking); React `<img>` with glyph fallback. No new frontend TS types (the endpoint returns image bytes).

---

## Context & constraints

- **beets has no artist images.** `fetchart` is album-cover-only; no bundled plugin fetches artist art. MusicDrop builds this itself.
- **External HTTP is not beets' job.** It lives in a new `app/artwork/` module — the beets adapter (`app/beets/`) stays beets-only (CLAUDE.md rule 3). The adapter only supplies the artist roster (names; MBIDs exist but are unused in v1).
- **Builds on the artist spine** (`feat/artist-view`): the roster cards + `/artists/:name` header are the consumers. **Implement after the spine merges** — branch `feat/artist-images` off the new `main`.
- **Correctness over coverage.** Better to show the glyph than a wrong artist's face. Matching is the heart of this feature.
- **Self-hosted; be a polite Deezer client** (rate limits + caching).

## Decisions (from brainstorming)

1. **Source:** Deezer first, behind a pluggable `ArtistImageSource` interface.
2. **Matching (v1):** Deezer search + **strict name verification** (reject weak matches → glyph). No MBID use in v1.
3. **Sequencing:** incremental, **small chunks**; no Settings UI now (env-configured).
4. **Default:** **opt-in (off by default).**
5. **Rate-limited + polite** toward Deezer.
6. **Later chunks (not v1):** MBID-exact matching via MusicBrainz; the manual **edit/override cover**; a Settings UI; fanart.tv/MusicBrainz/Spotify sources.

## Components (`backend/app/artwork/`)

- **`ArtistImageSource`** (Protocol): `async resolve(name: str) -> SourceResult | None`. Swappable; one impl now.
- **`DeezerArtistImageSource`**: `GET https://api.deezer.com/search/artist?q=<name>&limit=<N>` (no key) → **verify + pick** (see Matching) → download the chosen artist's `picture_xl` (fallback `picture_big`). Returns image bytes + content-type, or `None` when nothing verifies.
- **`ArtistImageCache`**: on-disk under `data/cache/artist-images/` (gitignored). Key = `sha1(normalized_name)`. Three slots per artist:
  - **override** (manual pin — written by the future edit-cover chunk; **always wins**, never clobbered by auto-fetch),
  - **positive** (the auto-fetched image file),
  - **negative** (a `.miss` marker + timestamp, TTL'd).
- **`ArtistImageService`**: orchestrates — override? → positive cache? → fresh negative? → else resolve via source under the rate limiter → store → return bytes+mime or `None`.
- **Shared:** an httpx async client (timeout + `User-Agent`) and a token-bucket rate limiter.

## Matching (Deezer, v1) — the core

The whole point is **not grabbing the wrong artist**:

1. **Normalize** both sides for comparison: trim, casefold, fold diacritics, collapse punctuation/whitespace (keep the original string for the query).
2. Query `search/artist?q=<name>&limit=<N>` (a handful of results, not just 1).
3. **Verify:** keep only results whose normalized name **equals** the normalized query (exact-after-normalization). Near-but-not-equal (e.g. "The Beatles" vs "Beatles") is *not* auto-accepted in v1.
4. **Pick** among verified matches by Deezer prominence (`nb_fan`, then `nb_album`) — the canonical artist, not a tribute/cover act.
5. **No verified match → return `None`** (→ negative cache → glyph). Never fall back to a fuzzy/top hit.

Mis-resolution is still possible (two real artists share a name) — that's what the **edit/override cover** chunk fixes later. v1 errs toward the glyph.

## Config (pydantic-settings, `MUSICDROP_` prefix)

- `ARTIST_IMAGES_ENABLED: bool = False` — opt-in.
- `ARTIST_IMAGE_SOURCE: str = "deezer"`.
- `ARTIST_IMAGE_CACHE_DIR` — default under `data/`.
- `ARTIST_IMAGE_RATE_PER_SEC` (default ~5), max in-flight concurrency (~2–3), negative-cache TTL (~7 days), Deezer result `limit` (~5). Conservative defaults. (Graduate into the Settings UI later.)

## Trigger & serving

`GET /api/artists/{name}/image` (name URL-encoded; switch to `?name=` query if the `%2F`-in-name edge bites):
1. Feature disabled → `404`.
2. Override or positive cache hit → serve bytes + content-type (+ cache headers).
3. Fresh negative cache → `404`.
4. Else resolve via the service (rate-limited): verified → store + serve; unverified/error → record negative cache → `404`.

Lazy on first view is fine **here** (the concern was matching, not the trigger): each artist resolves at most once, then is cached; the rate limiter shapes the first-load burst. Frontend `<img>` falls back to the glyph on `404` — same pattern as album covers. No background job in v1.

## Rate limiting & politeness (respect Deezer)

- Token-bucket limiter holds outbound Deezer calls well under ~50 req/5s (default ~5/s) + small max concurrency.
- Descriptive `User-Agent`; HTTP timeout so Deezer never blocks the page.
- Honor `429`/errors with backoff → transient no-image (glyph) + short negative-cache.
- Each artist resolves at most once (cached); steady-state volume ≈ zero.

## UI

- **`ArtistImage`** component: square `<img>` → the image endpoint, person-glyph fallback on error (mirrors album `CoverImage`).
- **Roster cards** (`ArtistsPage`): swap the glyph avatar for the square `ArtistImage` poster (album-cover treatment).
- **Artist header** (`ArtistAlbumsPage`): a larger `ArtistImage` poster beside the name (mirrors the album-detail cover header).
- Glyph remains the fallback everywhere (feature off / no verified match / error).

## Error handling

Source/network failure, timeout, `429`, malformed response → no image (`404`) → glyph. Never block/crash the page. Transient errors → short negative-cache; confirmed no-match → longer TTL.

## Build chunks (small, independently shippable)

1. **`app/artwork/` core (backend, no UI):** source Protocol + `DeezerArtistImageSource` (search + **strict verify**) + cache (override/positive/negative slots) + rate limiter + service. Unit-tested with mocked Deezer (verify accepts exact, rejects near/weak, picks by prominence; override wins; negative cache prevents repeat calls).
2. **Serve endpoint:** `GET /api/artists/{name}/image` + the opt-in flag. Tested (enabled/disabled, cache hit, miss).
3. **UI posters:** `ArtistImage` component + roster cards + artist header + glyph fallback. Tested + live screenshot.
4. **(Later) MBID-exact matching** via MusicBrainz for tagged artists.
5. **(Later) Edit/override cover:** UI to pick an alternate result / paste a URL / upload, writing the override slot.

## Testing

- Mock Deezer HTTP (respx): exact match → caches + serves; near-match ("Beatles" vs "The Beatles") → `None`/glyph; no result → `404` + negative cache (assert **no repeat HTTP**); override present → served without any HTTP; disabled → `404` no HTTP; rate limiter keeps calls under the cap.
- FE: `ArtistImage` renders + glyph fallback on `404`; roster + header use it.
- mypy strict; ruff.

## Dependencies & sequencing

- **Depends on the artist spine** (`feat/artist-view`) merging first — its roster cards + artist header are the consumers. Branch `feat/artist-images` off the new `main`; build chunks 1→3 (4–5 later).
- New backend dep: `httpx` (+ `respx` dev).

## Explicitly out of scope (even later)

Artist backgrounds/banners (portrait only); bulk re-scan/refresh UI; per-source quality ranking.
