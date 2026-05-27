# Import Chunk 4 — Candidate-Review Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the chunk-3 `/import/albums/:index` stub with the faithful per-album **candidate-review screen** — match header, candidate switcher, art-forward before/after album panels, a what-changes chip set, the full track diff (changed/missing/unmatched), and beets' real choice actions (Apply · Skip · Use as-is · As tracks) — backed by a small backend slice that adds **cover art** to the import contract.

**Architecture:** A **Stacked** single-column screen (header → before/after → tracklist → sticky action bar), consistent with `AlbumDetailPage`. It consumes the chunk-3 hooks `useImportCandidate(jobId, index, enabled)` + `useSubmitChoice(jobId)` at the route `/import/albums/:index?job=<id>` (jobId from the query, index from the path — both already carried by the chunk-3 seam). The backend slice adds two art sources to the contract: `Candidate.cover_after_url` (the matched MusicBrainz release's **Cover Art Archive** front URL, a string the browser fetches directly) and a `has_current_art` flag, plus a new `GET /api/import/{job}/albums/{index}/cover` endpoint that streams the **current files' embedded art** (read on demand from the parked album's source file — the worker is blocked while parked, so the file is still in place). Both images use the established serve-or-degrade pattern (an `<img>` whose `onError` flips to a placeholder).

**Tech Stack:** Backend — Python 3.11+, FastAPI + Pydantic, beets 2.11.0 (adapter boundary `app/beets/`), mediafile, pytest, mypy --strict, ruff. Frontend — React 19, react-router v7, Tailwind v4, shadcn/ui, TanStack Query v5, vitest + @testing-library/react + msw. Backend commands run from `backend/`, frontend from `frontend/`.

---

## Facts this plan encodes (verified against the tree on `feat/import`)

1. **The seam carries both ids.** Chunk 3 ships `ImportCandidatePage` at `/import/albums/:index?job=<id>`; it already reads `index` via `useParams<{ index }>()` and `job` via `useSearchParams()`. This plan replaces its body; the route + the Review link in the feed are unchanged.
2. **The chunk-3 hooks already exist** (`frontend/src/api/useImport.ts`): `useImportCandidate(jobId: string, index: number, enabled: boolean)` → `Candidate`; `useSubmitChoice(jobId)` → mutate `{ index, choice: ImportChoice }`, invalidates the job query, treats 404/409 as a quiet resolve. `Candidate`, `ImportChoice`, `Recommendation` are re-exported there from the generated schema. **Do not rebuild these.**
3. **`ImportAction`** (the contract) is `apply | skip | asis | astracks | abort`. Chunk 4 wires **apply/skip/asis/astracks**. `enter_id`/`search` are a later chunk (not in the enum). `abort` is in the enum but no Abort button is built here (it belongs with whole-import controls). `ImportChoice = { action, candidate_index?: number | null }`; `candidate_index` is the index into `Candidate.options` for `apply` (null ⇒ top match).
4. **`Candidate`** (`backend/app/models/import_models.py`) today: `confidence: float`, `recommendation: Recommendation`, `data_source: str | None`, `data_url: str | None`, `changed_fields: list[str]`, `album_before: AlbumChange`, `album_after: AlbumChange`, `tracks: list[TrackChange]`, `missing: list[MissingTrack]`, `unmatched: list[UnmatchedItem]`, `options: list[CandidateOption]`. `AlbumChange = { artist, album, year, label, country, media }` (all optional). `TrackChange = { index, status (unchanged|changed), title_before, title_after, track_before, track_after }`. `MissingTrack = { index, title }`. `UnmatchedItem = { title, track }`. `CandidateOption = { index, confidence, data_source, disambiguation }`.
5. **The mapping** (`backend/app/beets/import_mapping.py`) builds `Candidate` from a beets `AlbumMatch` in `map_album_match(...)`. `match.info` is an `AlbumInfo`; for MusicBrainz, `match.info.album_id` is the **release MBID** (UUID string) and `match.info.data_source == "MusicBrainz"`. `_opt_str`/`_opt_int`/`_confidence` are the existing helpers.
6. **Parking** (`backend/app/beets/import_session.py`): `WebImportSession.choose_match` maps the top match → `Candidate`, builds a `ParkedAlbum(album_index, folder, candidate)`, and calls `self.bridge.park(parked)` (which blocks the worker on a reply). `task.items` is the list of beets `Item`s for the album (each has `.path`, bytes); `task.cur_artist`/`task.cur_album` are the current identity. The `ImportBridge` holds reply slots keyed by `album_index`.
7. **The registry** (`backend/app/import_jobs/registry.py`): `candidate(job_id, index)` drains, then returns `row.parked.candidate` (`KeyError` if no parked album at `index`). `_FeedAlbum` is a **server-side dataclass** (`outcome`, `status`, `parked: ParkedAlbum | None`, `decided_action`) — the natural home for server-only data like the parked album's art source. `_drain_locked` sets `row.parked` from the bridge's out-queue.
8. **The existing cover pattern:** `backend/app/beets/library.py` `_cover_from_embedded(lib, album)` reads `MediaFile(track_path).images[0]` → `(bytes, mime)` (mime falls back to `application/octet-stream` so a bad image cleanly triggers the FE placeholder). `backend/app/api/albums.py` `GET /albums/{album_id}/cover` returns `Response(content=image_bytes, media_type=mime, headers={"Cache-Control": "public, max-age=3600"})` or `raise HTTPException(404)`. `mediafile.MediaFile` is the import.
9. **FE design vocabulary** (chunk 3 + `AlbumDetailPage.tsx`): page root `<article|section className="flex flex-col gap-8">` + `aria-label`; `BackLink` from `@/components/albums/album-grid`; shadcn `Table`/`Badge`/`Button`/`Separator`/`Skeleton`/`Card`; the serve-or-degrade cover is an `<img onError={()=>setFailed(true)}>` → `bg-muted` + `Music`/icon placeholder of the same size; `cn` from `@/lib/utils`; humanized recommendation labels live in `ImportPage.tsx` as `RECOMMENDATION_LABEL` (this plan **lifts that to `useImport.ts`** so both pages share it — Task 5).
10. **OpenAPI → types** (chunk-3 fact): regen the dump from `backend/` then `npm run gen:api` in `frontend/`. The dump command: `uv run python -c "import json; from app.main import app; from pathlib import Path; Path('../frontend/openapi.json').write_text(json.dumps(app.openapi()))"`.
11. **mypy:** `app/beets/*` modules are in the `[[tool.mypy.overrides]]` `disallow_untyped_calls=false` list; new test modules that build beets objects must be added there too (see `pyproject.toml`). Enums are `enum.StrEnum`.

---

## File structure

| File | Status | Responsibility |
| --- | --- | --- |
| `backend/app/models/import_models.py` | Modify | Add `cover_after_url: str \| None` and `has_current_art: bool` to `Candidate`. |
| `backend/app/beets/import_mapping.py` | Modify | Compute the CAA front URL from a MusicBrainz `AlbumInfo`; thread `has_current_art` + `cover_after_url` into `map_album_match`. Add `coverartarchive_front_url()` + `embedded_art()` helpers (pure/adapter). |
| `backend/app/beets/import_session.py` | Modify | At park time, detect current embedded art (`has_current_art`) and stash the source file path so the cover endpoint can serve it; pass `has_current_art` into the mapping. Add `art_source` to the parked state via the bridge (server-side, not on the Pydantic model). |
| `backend/app/import_jobs/registry.py` | Modify | Store the parked album's art source path on `_FeedAlbum`; add `candidate_cover(job_id, index) -> tuple[bytes, str] \| None`. |
| `backend/app/api/import_.py` | Modify | Add `GET /import/{job_id}/albums/{index}/cover` (the current-files embedded art; 404 when none). |
| `backend/tests/test_import_mapping.py` | Modify | CAA-URL + has_current_art mapping cases. |
| `backend/tests/test_import_cover.py` | Create | The cover endpoint + `candidate_cover` (embedded art present → bytes; none → 404; unknown job/index → 404). |
| `frontend/openapi.json`, `frontend/src/api/schema.d.ts` | Modify (regen) | Pick up `cover_after_url`/`has_current_art` + the cover path. |
| `frontend/src/api/useImport.ts` | Modify | Lift `RECOMMENDATION_LABEL` here (shared); add `importCoverUrl(jobId, index)` + `coverAfterUrl(candidate)` tiny helpers (URL builders, no new query). |
| `frontend/src/pages/import/ImportCandidatePage.tsx` | Replace | The full stacked review screen (header, switcher, before/after, chips, tracklist, sticky actions). |
| `frontend/src/pages/import/ImportCandidatePage.test.tsx` | Create | vitest + msw coverage of every section + each action. |
| `frontend/src/pages/import/ImportPage.tsx` | Modify | Import `RECOMMENDATION_LABEL` from `useImport.ts` instead of defining it locally (Task 5). |

Decomposition: backend art slice first (Tasks 1–4), then the screen built in two passes — identity/header/art/switcher (Task 5) then tracklist + actions (Task 6) — then tests (Task 7) and the green+screenshot gate (Task 8). The screen is one file with internal sub-components, matching `ImportPage.tsx`.

---

### Task 1: `cover_after_url` on the contract + the CAA mapping

**Files:**
- Modify: `backend/app/models/import_models.py`
- Modify: `backend/app/beets/import_mapping.py`
- Test: `backend/tests/test_import_mapping.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_import_mapping.py`:
```python
from app.beets.import_mapping import coverartarchive_front_url


def test_caa_url_for_musicbrainz_release() -> None:
    url = coverartarchive_front_url(
        data_source="MusicBrainz", album_id="abcd-1234"
    )
    assert url == "https://coverartarchive.org/release/abcd-1234/front-500"


def test_caa_url_none_for_non_musicbrainz_or_missing_id() -> None:
    assert coverartarchive_front_url(data_source="Discogs", album_id="x") is None
    assert coverartarchive_front_url(data_source="MusicBrainz", album_id=None) is None
    assert coverartarchive_front_url(data_source=None, album_id="x") is None
```

- [ ] **Step 2: Run it (red)**

Run (from `backend/`): `uv run pytest tests/test_import_mapping.py -k caa -q`
Expected: FAIL — `cannot import name 'coverartarchive_front_url'`.

- [ ] **Step 3: Add the field + the helper + thread it through the mapping**

In `backend/app/models/import_models.py`, add to `Candidate` (after `data_url`):
```python
    # The matched release's Cover Art Archive front-image URL (MusicBrainz only),
    # or None. The browser fetches it directly and falls back to a placeholder on
    # error — so a release with no CAA art degrades gracefully.
    cover_after_url: str | None
    # Whether the current files carry embedded cover art. Drives the "+ cover art"
    # change chip and whether the "before" panel attempts to load an image.
    has_current_art: bool
```

In `backend/app/beets/import_mapping.py`, add the helper (after `_confidence`):
```python
def coverartarchive_front_url(
    *, data_source: str | None, album_id: str | None
) -> str | None:
    """The Cover Art Archive front-image URL for a MusicBrainz release, or None.

    CAA is keyed by the MusicBrainz release MBID (``AlbumInfo.album_id``). Only
    MusicBrainz matches have one; other sources (Discogs, etc.) return None and
    the UI shows a placeholder. ``front-500`` is CAA's 500px thumbnail rendition.
    """
    if data_source is None or album_id is None:
        return None
    if data_source.strip().lower() != "musicbrainz":
        return None
    mbid = str(album_id).strip()
    if not mbid:
        return None
    return f"https://coverartarchive.org/release/{mbid}/front-500"
```

Then add a `has_current_art: bool = False` keyword parameter to `map_album_match` and set the two new `Candidate` fields:
```python
def map_album_match(
    match: AlbumMatch,
    *,
    cur_artist: str | None,
    cur_album: str | None,
    options: list[CandidateOption],
    recommendation: Recommendation = Recommendation.none,
    has_current_art: bool = False,
) -> Candidate:
    ...
    return Candidate(
        confidence=_confidence(match.distance),
        recommendation=recommendation,
        data_source=_opt_str(match.info.data_source),
        data_url=_opt_str(match.info.data_url),
        cover_after_url=coverartarchive_front_url(
            data_source=_opt_str(match.info.data_source),
            album_id=_opt_str(getattr(match.info, "album_id", None)),
        ),
        has_current_art=has_current_art,
        changed_fields=list(match.distance.generic_penalty_keys),
        album_before=_album_change_from_current(
            list(match.mapping.keys()) + list(match.extra_items),
            cur_artist,
            cur_album,
        ),
        album_after=_album_change_from_info(match.info),
        tracks=_track_changes(match),
        missing=_missing_tracks(match),
        unmatched=_unmatched_items(match),
        options=options,
    )
```

- [ ] **Step 4: Update existing mapping tests that construct/assert `Candidate`**

Any existing assertion in `test_import_mapping.py` that builds an expected `Candidate` or checks its field set must include `cover_after_url` and `has_current_art`. Run the file and fix fallout:
Run (from `backend/`): `uv run pytest tests/test_import_mapping.py -q`
Expected: PASS (the new cases + the updated existing ones).

- [ ] **Step 5: Typecheck + commit**

Run (from `backend/`):
```bash
uv run mypy && uv run ruff check && uv run ruff format
git add app/models/import_models.py app/beets/import_mapping.py tests/test_import_mapping.py
git commit -m "feat(import): expose the matched release's Cover Art Archive URL on Candidate"
```

---

### Task 2: detect current embedded art + stash its source at park

**Files:**
- Modify: `backend/app/beets/import_mapping.py` (the embedded-art reader — it's adapter code)
- Modify: `backend/app/beets/import_session.py`
- Test: `backend/tests/test_import_session.py`

The cover endpoint serves the **current files'** embedded art. The art lives at the parked album's first item path; the worker is **blocked** while the album is parked, so the file is still at its source location. We detect presence at park time (for `has_current_art`) and stash the source path so the endpoint can stream it on demand.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_import_session.py` (it already builds in-memory beets `Item`s and a `WebImportSession`; follow that file's existing fixtures):
```python
from app.beets.import_mapping import embedded_art


def test_embedded_art_none_for_missing_or_artless(tmp_path) -> None:
    # A path that does not exist -> None (no crash).
    assert embedded_art(str(tmp_path / "nope.mp3")) is None
```

And a bridge test asserting the parked album records its art source. Add a focused test that drives `choose_match` for an uncertain album (mirror the existing park test) and asserts the bridge exposes the source path:
```python
def test_park_records_art_source(monkeypatch) -> None:
    # Build an uncertain AlbumMatch the same way the existing park test does
    # (see test_choose_match_parks_uncertain in this file) and run choose_match
    # on a thread; then assert the bridge knows the parked album's art source.
    # ... (reuse the file's existing harness: fake task with items[0].path set) ...
    # After the worker parks at index 0:
    assert session.bridge.art_source(0) is not None  # the first item's path
```
> If the existing harness builds `task.items` without a real `.path`, set `items[0].path = os.fsencode(str(tmp_path / "a.flac"))` before driving `choose_match`. The assertion only checks the path was captured, not that art exists.

- [ ] **Step 2: Run it (red)**

Run (from `backend/`): `uv run pytest tests/test_import_session.py -k "art" -q`
Expected: FAIL — `cannot import name 'embedded_art'` / `ImportBridge has no attribute 'art_source'`.

- [ ] **Step 3: Implement**

In `backend/app/beets/import_mapping.py` add the reader (mirrors `library._cover_from_embedded`):
```python
import os

from mediafile import MediaFile


def embedded_art(path: str) -> tuple[bytes, str] | None:
    """The first embedded cover image ``(bytes, mime)`` for a media file, or None.

    Used to render the "before" (current files) cover during review. Returns None
    for a missing file, an unreadable file, or one with no embedded image. A
    declared-but-empty mime falls back to ``application/octet-stream`` so the FE
    ``<img>`` cleanly degrades to a placeholder rather than rendering garbage.
    """
    if not os.path.isfile(path):
        return None
    try:
        images = MediaFile(path).images
    except Exception:  # noqa: BLE001 — any mediafile read error => no art
        return None
    if not images:
        return None
    image = images[0]
    mime = _opt_str(image.mime_type) or "application/octet-stream"
    return bytes(image.data), mime
```

In `backend/app/beets/import_session.py`:

(a) Give `ImportBridge` an art-source store (server-side; bytes/paths never touch the Pydantic models). Add to `__init__`: `self._art_source: dict[int, str] = {}`. Extend `park` to accept the source path and record it:
```python
    def park(self, parked: ParkedAlbum, art_source: str | None = None) -> ImportChoice:
        reply: queue.Queue[ImportChoice] = queue.Queue(maxsize=1)
        with self._lock:
            self._replies[parked.album_index] = reply
            if art_source is not None:
                self._art_source[parked.album_index] = art_source
            self._pending += 1
        self._out.put(parked)
        choice = reply.get()
        with self._lock:
            self._replies.pop(parked.album_index, None)
            self._pending -= 1
        return choice

    def art_source(self, album_index: int) -> str | None:
        """The current-files art source path for a parked album, or None."""
        with self._lock:
            return self._art_source.get(album_index)
```

(b) In `choose_match`, before parking, compute the first item's path + `has_current_art`, pass both through:
```python
        # The current files' first item supplies the "before" cover. Detect art
        # now (the worker is about to block parked, so the file is still here).
        items = list(task.items or [])
        art_source = os.fsdecode(items[0].path) if items and items[0].path else None
        has_current_art = (
            art_source is not None and embedded_art(art_source) is not None
        )
        top = candidates[0]
        options = map_candidate_options(candidates)
        candidate = map_album_match(
            top,
            cur_artist=task.cur_artist,
            cur_album=task.cur_album,
            options=options,
            recommendation=recommendation,
            has_current_art=has_current_art,
        )
        folder = self._task_folder(task)
        self.bridge.note_outcome(
            self._outcome(index, task, recommendation, AlbumOutcomeStatus.needs_review, match=top)
        )
        choice = self.bridge.park(
            ParkedAlbum(album_index=index, folder=folder, candidate=candidate),
            art_source=art_source,
        )
        return self._apply_choice(choice, candidates)
```
Add `from app.beets.import_mapping import embedded_art` to the existing import block from that module.

- [ ] **Step 4: Run it (green) + the whole session file**

Run (from `backend/`): `uv run pytest tests/test_import_session.py -q`
Expected: PASS. (Existing park/strong/skip tests stay green — the returned beets values are unchanged; only `has_current_art` flows in and the art source is recorded.)

- [ ] **Step 5: Typecheck + commit**

```bash
uv run mypy && uv run ruff check && uv run ruff format
git add app/beets/import_mapping.py app/beets/import_session.py tests/test_import_session.py
git commit -m "feat(import): detect current embedded art and stash its source at park"
```

---

### Task 3: the current-art cover endpoint

**Files:**
- Modify: `backend/app/import_jobs/registry.py`
- Modify: `backend/app/api/import_.py`
- Test: `backend/tests/test_import_cover.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_import_cover.py`. Drive a fake-runner import (mirror `tests/test_import_api.py`'s setup: `reset_registry(FakeImportRunner(parked=[...]))`, then `client.post("/api/import", ...)` and `client.get` the album) so an album is parked at index 0, with the parked album wired to a source file that has embedded art. The simplest hermetic approach is to assert the registry method directly + the endpoint's 404 paths:
```python
from fastapi.testclient import TestClient

from app.import_jobs.registry import reset_registry
# Use the same fake-runner harness as tests/test_import_api.py.


def test_cover_404_when_no_parked_album(client: TestClient) -> None:
    # Start a job whose albums are all auto-applied (none parked) via the fake
    # runner, then request a cover for an index that isn't parked.
    ...
    r = client.get(f"/api/import/{job_id}/albums/0/cover")
    assert r.status_code == 404


def test_cover_404_for_unknown_job(client: TestClient) -> None:
    r = client.get("/api/import/nope/albums/0/cover")
    assert r.status_code == 404


def test_cover_streams_embedded_art(client: TestClient, tmp_path) -> None:
    # Park an album whose art_source is a file with embedded art; assert 200 +
    # the bytes + a sane content-type. Build the source file by writing a tiny
    # tagged media fixture, OR monkeypatch app.beets.import_mapping.embedded_art
    # to return (b"PNGDATA", "image/png") for the known source path.
    ...
    r = client.get(f"/api/import/{job_id}/albums/{idx}/cover")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.content == b"PNGDATA"
```
> Prefer the monkeypatch route for `test_cover_streams_embedded_art` (`monkeypatch.setattr("app.import_jobs.registry.embedded_art", lambda p: (b"PNGDATA", "image/png"))`) so the test needs no real audio fixture. The `FakeImportRunner` must set the parked album's `art_source` — see Step 3 for how the registry reads it from the bridge; the fake drives the real `ImportBridge`, so have the fake call `bridge.park(parked, art_source="/fake/path")` (or extend the fake to pass an art source). If the fake can't set it, assert via the registry's `candidate_cover` with a directly-constructed job instead.

- [ ] **Step 2: Run it (red)**

Run (from `backend/`): `uv run pytest tests/test_import_cover.py -q`
Expected: FAIL (endpoint 404s for everything / `candidate_cover` missing).

- [ ] **Step 3: Implement the registry method + the endpoint**

In `backend/app/import_jobs/registry.py`:
- Add `from app.beets.import_mapping import embedded_art` at the top.
- Add the art source to `_FeedAlbum`: `art_source: str | None = None`.
- In `_drain_locked`, when setting `row.parked`, also capture the art source from the bridge:
```python
            row = job.albums.get(parked.album_index)
            if row is not None:
                row.parked = parked
                row.art_source = job.bridge.art_source(parked.album_index)
```
- Add the accessor:
```python
    def candidate_cover(self, job_id: str, index: int) -> tuple[bytes, str] | None:
        """Embedded cover art for the parked album at ``index``, or None.

        Reads the current files' first item on demand (the worker is parked, so
        the source is still in place). KeyError when the job/album is unknown or
        not parked — the API maps that to 404, same as the Candidate route.
        """
        self.drain(job_id)
        job = self._require(job_id)
        with self._lock:
            row = job.albums.get(index)
            if row is None or row.parked is None or row.art_source is None:
                raise KeyError(index)
            source = row.art_source
        return embedded_art(source)  # read outside the lock (file I/O)
```

In `backend/app/api/import_.py` add the endpoint (after the choice route), importing `Response`:
```python
from fastapi import APIRouter, Depends, HTTPException, Path, Response, status


@router.get("/import/{job_id}/albums/{index}/cover")
async def get_import_album_cover(
    job_id: str,
    index: Annotated[int, Path(ge=0)],
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> Response:
    try:
        cover = reg.candidate_cover(job_id, index)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import album not found"
        ) from None
    if cover is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No cover art"
        ) from None
    image_bytes, mime = cover
    return Response(
        content=image_bytes,
        media_type=mime,
        headers={"Cache-Control": "no-store"},  # parked-album art is transient
    )
```

- [ ] **Step 4: Run it (green) + the import suite**

Run (from `backend/`): `uv run pytest tests/test_import_cover.py tests/test_import_api.py tests/test_import_registry.py -q`
Expected: PASS.

- [ ] **Step 5: Typecheck + commit**

```bash
uv run mypy && uv run ruff check && uv run ruff format
git add app/import_jobs/registry.py app/api/import_.py tests/test_import_cover.py
git commit -m "feat(import): serve the parked album's current embedded cover art"
```
> If `tests/test_import_cover.py` needs the mypy override, add `tests.test_import_cover` to the `[[tool.mypy.overrides]]` module list in `pyproject.toml` and include it in the commit.

---

### Task 4: regenerate the API types

**Files:**
- Modify (regen): `frontend/openapi.json`, `frontend/src/api/schema.d.ts`

- [ ] **Step 1: Regenerate the dump**

Run (from `backend/`):
```bash
uv run python -c "import json; from app.main import app; from pathlib import Path; Path('../frontend/openapi.json').write_text(json.dumps(app.openapi()))"
```
Verify the new surface landed (from `backend/`):
```bash
grep -c "cover_after_url\|has_current_art" ../frontend/openapi.json
grep -c "albums/{index}/cover" ../frontend/openapi.json
```
Expected: both `>= 1`.

- [ ] **Step 2: Regen types + typecheck**

Run (from `frontend/`):
```bash
npm run gen:api
grep -c "cover_after_url" src/api/schema.d.ts
npx tsc -b --noEmit
```
Expected: `cover_after_url` present; `tsc` clean (the existing typed `Candidate` usages still compile — the two new fields are additive).

- [ ] **Step 3: Commit**

```bash
git add frontend/openapi.json frontend/src/api/schema.d.ts
git commit -m "feat(import): regenerate API types for cover art on Candidate"
```

---

### Task 5: review screen — header, source, what-changes, before/after, switcher

**Files:**
- Modify: `frontend/src/api/useImport.ts` (lift `RECOMMENDATION_LABEL` here + add URL helpers)
- Modify: `frontend/src/pages/import/ImportPage.tsx` (import the shared label)
- Replace: `frontend/src/pages/import/ImportCandidatePage.tsx` (the identity half of the screen)

This task ships the top of the stacked screen; Task 6 adds the tracklist + actions; Task 7 tests it. The page reads `index` from the path and `job` from the query (both already there in the stub), then `useImportCandidate(jobId, index, enabled)`.

- [ ] **Step 1: Share the recommendation labels + add URL helpers in `useImport.ts`**

In `frontend/src/api/useImport.ts`, after the `Recommendation` re-export, add:
```ts
/** Humanized labels for beets' recommendation levels (shared by the feed +
 * the review screen). Keeps the raw enum ("strong"/"none") out of the UI. */
export const RECOMMENDATION_LABEL: Record<Recommendation, string> = {
  none: "No match",
  low: "Low match",
  medium: "Medium match",
  strong: "Strong match",
};

/** URL of the current files' embedded cover for a parked album (served by the
 * backend; the browser falls back to a placeholder on 404). */
export function importCoverUrl(jobId: string, index: number): string {
  return `/api/import/${jobId}/albums/${index}/cover`;
}
```
Then in `frontend/src/pages/import/ImportPage.tsx`, delete the local `const RECOMMENDATION_LABEL = {...}` and import it: add `RECOMMENDATION_LABEL` to the existing `@/api/useImport` import. Run `npx tsc -b --noEmit` to confirm the move compiles.

- [ ] **Step 2: Replace the stub with the identity half**

Replace `frontend/src/pages/import/ImportCandidatePage.tsx` entirely:
```tsx
import { AlertCircle, ChevronDown, ExternalLink, Music } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import type { Candidate } from "@/api/useImport";
import {
  importCoverUrl,
  RECOMMENDATION_LABEL,
  useImportCandidate,
  useSubmitChoice,
} from "@/api/useImport";
import { BackLink } from "@/components/albums/album-grid";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export function ImportCandidatePage() {
  const { index: indexParam } = useParams<{ index: string }>();
  const [searchParams] = useSearchParams();
  const jobId = searchParams.get("job") ?? undefined;
  const index = Number(indexParam);
  const validIndex = Number.isInteger(index) && index >= 0;

  // Without a job id (deep link lost the query) or a bad index, there's nothing
  // to fetch — send the user back to the import feed.
  const enabled = Boolean(jobId) && validIndex;
  const { data, isPending, isError, refetch } = useImportCandidate(
    jobId ?? "",
    validIndex ? index : 0,
    enabled,
  );

  const backTo = jobId ? `/import?job=${jobId}` : "/import";

  if (!enabled) {
    return (
      <Shell backTo={backTo}>
        <Notice
          title="Nothing to review"
          body="This review link is missing its import job. Go back to the import."
        />
      </Shell>
    );
  }
  if (isPending) {
    return (
      <Shell backTo={backTo}>
        <CandidateSkeleton />
      </Shell>
    );
  }
  if (isError) {
    // A 404 here means the album is no longer parked (already decided / the
    // worker advanced). Treat it as "return to the feed", not a hard error.
    return (
      <Shell backTo={backTo}>
        <Notice
          title="This album isn’t waiting for review"
          body="It may already be decided. Head back to the import to see the feed."
          onRetry={() => void refetch()}
        />
      </Shell>
    );
  }

  return (
    <Shell backTo={backTo}>
      <ReviewScreen
        candidate={data}
        jobId={jobId as string}
        index={index}
        backTo={backTo}
      />
    </Shell>
  );
}

/** Page chrome: the up-link to the feed. */
function Shell({ backTo, children }: { backTo: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-6" aria-label="Review album">
      <BackLink to={backTo} label="Import" />
      {children}
    </section>
  );
}

function ReviewScreen({
  candidate,
  jobId,
  index,
  backTo,
}: {
  candidate: Candidate;
  jobId: string;
  index: number;
  backTo: string;
}) {
  // The candidate index the user will Apply — defaults to the top match (0).
  const [selected, setSelected] = useState(0);

  return (
    <div className="flex flex-col gap-6">
      <MatchHeader candidate={candidate} />
      {candidate.options.length > 1 && (
        <CandidateSwitcher
          options={candidate.options}
          selected={selected}
          onSelect={setSelected}
        />
      )}
      <BeforeAfter candidate={candidate} jobId={jobId} index={index} />
      <WhatChanges candidate={candidate} />
      {/* Task 6 inserts <TrackDiff/> and the sticky <ReviewActions/> here,
          using `selected`, `jobId`, `index`, and `backTo`. */}
    </div>
  );
}

/** `<confidence>% · <recommendation>` + Artist — Album + the source line. */
function MatchHeader({ candidate }: { candidate: Candidate }) {
  const after = candidate.album_after;
  const sourceBits = [
    candidate.data_source,
    after.year?.toString() ?? null,
    after.media,
    after.country,
    after.label,
  ].filter((b): b is string => Boolean(b));
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h2 className="text-2xl font-semibold tracking-tight">
          {after.artist ?? "Unknown artist"} — {after.album ?? "Unknown album"}
        </h2>
      </div>
      <p className="text-muted-foreground flex items-center gap-2 text-sm">
        <span className="text-foreground font-medium">
          {Math.round(candidate.confidence)}%
        </span>
        · {RECOMMENDATION_LABEL[candidate.recommendation]}
        {sourceBits.length > 0 && <span aria-hidden="true">·</span>}
        <span className="truncate">{sourceBits.join(" · ")}</span>
        {candidate.data_url && (
          <a
            href={candidate.data_url}
            target="_blank"
            rel="noreferrer"
            className="text-foreground inline-flex items-center gap-1 underline underline-offset-4"
          >
            view <ExternalLink className="size-3" aria-hidden="true" />
          </a>
        )}
      </p>
    </div>
  );
}

/** Pick a different ranked release to Apply. A native <select> styled as a
 * control — the option text carries % + source + disambiguation. */
function CandidateSwitcher({
  options,
  selected,
  onSelect,
}: {
  options: Candidate["options"];
  selected: number;
  onSelect: (index: number) => void;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-sm font-medium">Other candidates ({options.length})</span>
      <div className="relative w-full max-w-md">
        <select
          value={selected}
          onChange={(e) => onSelect(Number(e.target.value))}
          aria-label="Candidate release"
          className="border-input bg-background w-full appearance-none rounded-md border px-3 py-2 pr-9 text-sm"
        >
          {options.map((opt) => (
            <option key={opt.index} value={opt.index}>
              {Math.round(opt.confidence)}% · {opt.data_source ?? "?"}
              {opt.disambiguation ? ` · ${opt.disambiguation}` : ""}
            </option>
          ))}
        </select>
        <ChevronDown
          className="text-muted-foreground pointer-events-none absolute top-1/2 right-3 size-4 -translate-y-1/2"
          aria-hidden="true"
        />
      </div>
    </label>
  );
}

/** Two calm panels: NOW (your files) vs AFTER import, art-forward. */
function BeforeAfter({
  candidate,
  jobId,
  index,
}: {
  candidate: Candidate;
  jobId: string;
  index: number;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      <AlbumPanel
        heading="Now (your files)"
        change={candidate.album_before}
        coverUrl={candidate.has_current_art ? importCoverUrl(jobId, index) : null}
        changedFields={[]}
      />
      <AlbumPanel
        heading="After import"
        change={candidate.album_after}
        coverUrl={candidate.cover_after_url}
        changedFields={candidate.changed_fields}
      />
    </div>
  );
}

function AlbumPanel({
  heading,
  change,
  coverUrl,
  changedFields,
}: {
  heading: string;
  change: Candidate["album_after"];
  coverUrl: string | null;
  changedFields: string[];
}) {
  const changed = new Set(changedFields);
  return (
    <div className="border-border flex flex-col gap-3 rounded-xl border p-4">
      <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
        {heading}
      </p>
      <Cover url={coverUrl} />
      <div className="flex flex-col gap-0.5">
        <Field label="Album" value={change.album} changed={changed.has("album")} />
        <Field label="Artist" value={change.artist} changed={changed.has("artist")} />
        <Field
          label="Year"
          value={change.year?.toString() ?? null}
          changed={changed.has("year")}
        />
        <Field label="Label" value={change.label} changed={changed.has("label")} />
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  changed,
}: {
  label: string;
  value: string | null;
  changed: boolean;
}) {
  return (
    <div className="flex items-baseline gap-2 text-sm">
      <span className="text-muted-foreground w-12 shrink-0">{label}</span>
      <span className={cn("truncate", changed && "text-foreground font-medium")}>
        {value ?? "—"}
      </span>
      {changed && (
        <Badge variant="secondary" className="shrink-0">
          changed
        </Badge>
      )}
    </div>
  );
}

/** Serve-or-degrade cover (mirrors AlbumDetailPage.CoverImage). */
function Cover({ url }: { url: string | null }) {
  const [failed, setFailed] = useState(false);
  if (url === null || failed) {
    return (
      <div
        className="bg-muted flex aspect-square w-full items-center justify-center rounded-lg"
        aria-hidden="true"
      >
        <Music className="text-muted-foreground size-10" />
      </div>
    );
  }
  return (
    <img
      src={url}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
      className="bg-muted aspect-square w-full rounded-lg object-cover"
    />
  );
}

/** One-line chip set of what import will change. */
function WhatChanges({ candidate }: { candidate: Candidate }) {
  const chips: string[] = [];
  if (candidate.cover_after_url && !candidate.has_current_art) {
    chips.push("+ cover art");
  }
  for (const f of candidate.changed_fields) {
    chips.push(f);
  }
  const changedTracks = candidate.tracks.filter((t) => t.status === "changed").length;
  if (changedTracks > 0) {
    chips.push(`${changedTracks} of ${candidate.tracks.length} titles`);
  }
  if (chips.length === 0) {
    return null;
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-muted-foreground text-sm">Changes</span>
      {chips.map((c) => (
        <Badge key={c} variant="outline">
          {c}
        </Badge>
      ))}
    </div>
  );
}

function Notice({
  title,
  body,
  onRetry,
}: {
  title: string;
  body: string;
  onRetry?: () => void;
}) {
  return (
    <div className="border-border flex flex-col items-center gap-3 rounded-xl border border-dashed py-16 text-center">
      <AlertCircle className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">{title}</p>
        <p className="text-muted-foreground text-sm">{body}</p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

function CandidateSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-hidden="true">
      <p className="sr-only" role="status">
        Loading the proposed match…
      </p>
      <div className="flex flex-col gap-2">
        <Skeleton className="h-7 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Skeleton className="h-64 rounded-xl" />
        <Skeleton className="h-64 rounded-xl" />
      </div>
    </div>
  );
}
```
> Note `Separator` is imported for Task 6 (the tracklist divider); if `tsc`'s `noUnusedLocals` flags it in this task, add the `<Separator />` in Task 6 or drop the import until then. Keeping it here documents intent — remove if the build complains.

- [ ] **Step 3: Typecheck**

Run (from `frontend/`): `npx tsc -b --noEmit`
Expected: PASS. (If `Separator` is flagged unused, remove it from the import for now.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/useImport.ts frontend/src/pages/import/ImportPage.tsx frontend/src/pages/import/ImportCandidatePage.tsx
git commit -m "feat(import): candidate-review header, source, before/after art, switcher"
```

---

### Task 6: review screen — full tracklist + the sticky action bar

**Files:**
- Modify: `frontend/src/pages/import/ImportCandidatePage.tsx`

- [ ] **Step 1: Add the track diff + actions, wire them into `ReviewScreen`**

In `ImportCandidatePage.tsx`, import the table primitives + `useSubmitChoice` is already imported; add icons. Insert `<TrackDiff candidate={candidate} />` and the sticky `<ReviewActions/>` where Task 5 left the comment in `ReviewScreen`:
```tsx
      <Separator />
      <TrackDiff candidate={candidate} />
      <ReviewActions
        jobId={jobId}
        index={index}
        selected={selected}
        backTo={backTo}
      />
```
Add these components (and the `Table*` imports from `@/components/ui/table`, plus `Check`, `Loader2`, `Plus`, `Minus`, `Pencil` from `lucide-react`):
```tsx
/** Every track current→proposed; changed rows tagged, missing/unmatched flagged. */
function TrackDiff({ candidate }: { candidate: Candidate }) {
  return (
    <section aria-label="Track changes" className="flex flex-col gap-3">
      <h3 className="text-sm font-medium">Tracklist · {candidate.tracks.length}</h3>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-12 pr-4 text-right">#</TableHead>
            <TableHead>Now</TableHead>
            <TableHead>After import</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {candidate.tracks.map((t, i) => {
            const changed = t.status === "changed";
            return (
              <TableRow key={t.index ?? `row-${i}`} className={cn(changed && "bg-primary/5")}>
                <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
                  {t.track_after ?? t.track_before ?? "–"}
                </TableCell>
                <TableCell className="text-muted-foreground">
                  <span className="truncate">{t.title_before ?? "—"}</span>
                </TableCell>
                <TableCell>
                  <span className="flex min-w-0 items-center gap-2">
                    <span className={cn("truncate", changed && "font-medium")}>
                      {t.title_after ?? "—"}
                    </span>
                    {changed && (
                      <Pencil className="text-muted-foreground size-3 shrink-0" aria-label="changed" />
                    )}
                  </span>
                </TableCell>
              </TableRow>
            );
          })}
          {candidate.missing.map((m, i) => (
            <TableRow key={`missing-${m.index ?? i}`}>
              <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
                {m.index ?? "–"}
              </TableCell>
              <TableCell className="text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <Minus className="size-3" aria-hidden="true" /> missing
                </span>
              </TableCell>
              <TableCell className="text-muted-foreground">{m.title ?? "—"}</TableCell>
            </TableRow>
          ))}
          {candidate.unmatched.map((u, i) => (
            <TableRow key={`unmatched-${i}`}>
              <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
                –
              </TableCell>
              <TableCell>
                <span className="inline-flex items-center gap-1">
                  <Plus className="size-3" aria-hidden="true" /> {u.title ?? "—"}
                </span>
              </TableCell>
              <TableCell className="text-muted-foreground">not on release</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}

/** The beets choose_match actions, in a sticky bottom bar. Apply uses the
 * selected candidate index; on success we return to the feed (the worker
 * advances to the next album). */
function ReviewActions({
  jobId,
  index,
  selected,
  backTo,
}: {
  jobId: string;
  index: number;
  selected: number;
  backTo: string;
}) {
  const navigate = useNavigate();
  const submit = useSubmitChoice(jobId);

  function decide(action: "apply" | "skip" | "asis" | "astracks") {
    submit.mutate(
      {
        index,
        choice: {
          action,
          candidate_index: action === "apply" ? selected : null,
        },
      },
      { onSuccess: () => navigate(backTo) },
    );
  }

  return (
    <div className="bg-background/90 sticky bottom-0 -mx-2 flex flex-wrap items-center justify-end gap-2 border-t px-2 py-3 backdrop-blur">
      <Button
        variant="ghost"
        size="sm"
        disabled={submit.isPending}
        onClick={() => decide("skip")}
      >
        Skip
      </Button>
      <Button
        variant="outline"
        size="sm"
        disabled={submit.isPending}
        onClick={() => decide("asis")}
      >
        Use as-is
      </Button>
      <Button
        variant="outline"
        size="sm"
        disabled={submit.isPending}
        onClick={() => decide("astracks")}
      >
        As tracks
      </Button>
      <Button disabled={submit.isPending} onClick={() => decide("apply")}>
        {submit.isPending ? (
          <>
            <Loader2 className="animate-spin" aria-hidden="true" /> Applying…
          </>
        ) : (
          <>
            <Check aria-hidden="true" /> Apply
          </>
        )}
      </Button>
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

Run (from `frontend/`): `npx tsc -b --noEmit`
Expected: PASS (all imports now used, incl. `Separator`).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/import/ImportCandidatePage.tsx
git commit -m "feat(import): candidate-review full track diff + sticky choice actions"
```

---

### Task 7: review-screen behavioral tests

**Files:**
- Create: `frontend/src/pages/import/ImportCandidatePage.test.tsx`

- [ ] **Step 1: Write the tests**

Create `frontend/src/pages/import/ImportCandidatePage.test.tsx`. Use `renderWithProviders(ui, { route, path })` with `route="/import/albums/1?job=job-1"` and `path="/import/albums/:index"` so `useParams`/`useSearchParams` resolve. Mock the candidate GET + assert the choice POST body.
```tsx
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { Candidate } from "@/api/useImport";
import { ImportCandidatePage } from "@/pages/import/ImportCandidatePage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const CANDIDATE_URL = `${window.location.origin}/api/import/job-1/albums/1`;
const CHOICE_URL = `${window.location.origin}/api/import/job-1/albums/1/choice`;

function makeCandidate(overrides: Partial<Candidate> = {}): Candidate {
  return {
    confidence: 76,
    recommendation: "medium",
    data_source: "MusicBrainz",
    data_url: "https://musicbrainz.org/release/abc",
    cover_after_url: "https://coverartarchive.org/release/abc/front-500",
    has_current_art: false,
    changed_fields: ["album", "label"],
    album_before: { artist: "Radiohead", album: "OK Computr", year: null, label: null, country: null, media: null },
    album_after: { artist: "Radiohead", album: "OK Computer", year: 1997, label: "Parlophone", country: "GB", media: "CD" },
    tracks: [
      { index: 1, status: "unchanged", title_before: "Airbag", title_after: "Airbag", track_before: 1, track_after: 1 },
      { index: 2, status: "changed", title_before: "Paranoid Andrid", title_after: "Paranoid Android", track_before: 2, track_after: 2 },
    ],
    missing: [{ index: 10, title: "Lull" }],
    unmatched: [{ title: "bonus.mp3", track: null }],
    options: [
      { index: 0, confidence: 76, data_source: "MusicBrainz", disambiguation: "1997 UK CD" },
      { index: 1, confidence: 71, data_source: "MusicBrainz", disambiguation: "2008 reissue" },
    ],
    ...overrides,
  };
}

function renderAt(route = "/import/albums/1?job=job-1") {
  return renderWithProviders(<ImportCandidatePage />, { route, path: "/import/albums/:index" });
}

describe("ImportCandidatePage", () => {
  test("renders header, source, what-changes, before/after and the tracklist", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    expect(
      await screen.findByRole("heading", { name: /Radiohead — OK Computer/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/76%/)).toBeInTheDocument();
    expect(screen.getByText(/Medium match/i)).toBeInTheDocument();
    // what-changes chips
    expect(screen.getByText("+ cover art")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 titles")).toBeInTheDocument();
    // before vs after album title both present
    expect(screen.getByText("OK Computr")).toBeInTheDocument();
    expect(screen.getByText("OK Computer")).toBeInTheDocument();
    // tracklist incl. missing + unmatched
    expect(screen.getByText("Paranoid Android")).toBeInTheDocument();
    expect(screen.getByText("Lull")).toBeInTheDocument();
    expect(screen.getByText("bonus.mp3")).toBeInTheDocument();
  });

  test("Apply posts apply + the top candidate index, then returns to the feed", async () => {
    let body: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await user.click(await screen.findByRole("button", { name: /^Apply/i }));
    await waitFor(() => expect(body).toEqual({ action: "apply", candidate_index: 0 }));
  });

  test("switching candidate then Apply posts the chosen index", async () => {
    let body: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await user.selectOptions(
      await screen.findByLabelText(/candidate release/i),
      "1",
    );
    await user.click(screen.getByRole("button", { name: /^Apply/i }));
    await waitFor(() => expect(body).toEqual({ action: "apply", candidate_index: 1 }));
  });

  test("Skip posts skip (no candidate index)", async () => {
    let body: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await user.click(await screen.findByRole("button", { name: /^Skip/i }));
    await waitFor(() => expect(body).toEqual({ action: "skip", candidate_index: null }));
  });

  test("a 404 (no longer parked) shows the calm 'not waiting' notice", async () => {
    server.use(
      http.get(CANDIDATE_URL, () =>
        HttpResponse.json({ detail: "Import album not found" }, { status: 404 }),
      ),
    );
    renderAt();
    expect(
      await screen.findByText(/isn.t waiting for review/i),
    ).toBeInTheDocument();
  });

  test("a missing job id sends the user back with a notice", async () => {
    // No ?job= -> nothing to fetch (the GET handler is never hit).
    renderAt("/import/albums/1");
    expect(screen.getByText(/nothing to review/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Import" })).toHaveAttribute("href", "/import");
  });
});
```

- [ ] **Step 2: Run the tests (green)**

Run (from `frontend/`): `npx vitest run src/pages/import/ImportCandidatePage.test.tsx`
Expected: PASS (all cases). If an assertion mismatches the rendered output, fix the **test** to the real output (the screen passed spec + code review) unless it reveals a genuine bug — then STOP and report.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/import/ImportCandidatePage.test.tsx
git commit -m "test(import): cover the candidate-review screen + choice actions"
```

---

### Task 8: full green + live screenshot

**Files:** none (verification only).

- [ ] **Step 1: Backend suite + typecheck + lint**

Run (from `backend/`): `uv run pytest -q && uv run mypy && uv run ruff check && uv run ruff format --check`
Expected: all green.

- [ ] **Step 2: Frontend suite + typecheck + build**

Run (from `frontend/`): `npm run test && npm run typecheck && npm run build`
Expected: all green (the new `ImportCandidatePage` tests + everything prior).

- [ ] **Step 3: Live screenshot**

With the backend (`uv run uvicorn app.main:app --port 3030`) + frontend (`npm run dev`) running, drive a real or fake-backed parked album and screenshot the review screen at `/import/albums/:index?job=<id>`: confirm the header/source, the before/after panels (current art or placeholder → the CAA cover), the what-changes chips, the full tracklist with a changed row + a missing + an unmatched row, and the sticky action bar. Confirm Apply/Skip return to the feed. No console errors. (No commit — verification.)

> Triggering a real import mutates the user's library — only do so against a throwaway folder, or screenshot via the fake-runner path / mocked candidate. Do **not** run a real import against the user's library without explicit approval.

---

## Self-review checklist

**1. Spec coverage** (spec §"Candidate-review screen"): Match header (%, recommendation, source, data_url) → Task 5 `MatchHeader`. Candidate switcher (ranked options) → Task 5 `CandidateSwitcher` (+ Apply uses the selected index, Task 6). Before→After art-forward (covers + identity fields, changed marked, "+ cover art") → Tasks 1–3 (art backend) + Task 5 `BeforeAfter`/`AlbumPanel`/`Cover` + `WhatChanges`. Full tracklist (every track, changed tagged, missing/unmatched flagged) → Task 6 `TrackDiff`. Actions (Apply/Skip/Use as-is/As tracks) → Task 6 `ReviewActions`. Enter-id/search-again **deferred** (not in `ImportAction`) — out of scope, stated below.

**2. Placeholder scan:** every code step has complete, runnable code or an explicit, bounded instruction (the two test files name the exact harness to reuse). No "TBD"/"handle errors"/prose-only code steps.

**3. Type consistency:** `cover_after_url`/`has_current_art` added in Task 1 (backend model) flow through the regen (Task 4) to the FE `Candidate` (Tasks 5–7). `RECOMMENDATION_LABEL`/`importCoverUrl` defined in Task 5 (`useImport.ts`), consumed by the page. `ReviewScreen` passes `selected`/`jobId`/`index`/`backTo` into `ReviewActions` (Task 6) exactly as named. `coverartarchive_front_url`/`embedded_art` defined in Tasks 1/2 and consumed by the mapping/session/registry. `candidate_cover` defined in Task 3 (registry) and called by the endpoint.

**4. Convention consistency:** the screen reuses the app idiom (section/h2/`aria-label`, `BackLink`, serve-or-degrade `<img onError>`, shadcn `Table`/`Badge`/`Button`, `bg-primary/5` for changed rows — the same tint chunk 3 uses for needs-review, `cn`). Hooks reused from chunk 3 (no new query). The cover endpoint mirrors `albums.py`'s `Response(bytes, mime)` + the `_cover_from_embedded` reader. Tests follow the msw + `renderWithProviders` pattern.

**5. The art seam is honest:** `has_current_art` gates the "+ cover art" chip and whether the before panel attempts an image; the after cover is the real CAA URL (None for non-MusicBrainz → placeholder). Both images degrade to a placeholder on error.

---

## Out of scope (chunk 4 does NOT build these)

- **Per-candidate diff on switch.** The switcher picks which ranked release to **Apply** (informed by %/source/disambiguation), but the before/after + tracklist always reflect the **top** match's diff (the only one mapped into `Candidate`). Re-fetching a selected candidate's full diff (a `?candidate=<i>` variant that re-maps an alternate `AlbumMatch`) is a later enhancement — it needs the parked task's beets candidates retained for re-mapping.
- **Enter-MusicBrainz-id / Search-again.** Not in the `ImportAction` enum; a later chunk adds `enter_id`/`search` end-to-end.
- **An Abort button.** `abort` exists in the contract + `useSubmitChoice` can send it, but a whole-import Abort control is not part of this screen.
- **Duplicate resolution UI** (Skip/Keep/Remove/Merge) — `resolve_duplicate` is a no-op today; its own later chunk.
- **The done-summary polish + real-files end-to-end walkthrough** — chunk 5 (incl. adding a `skipped` count to `ImportProgress` and the `aria-live` throttle deferred from chunk 3).
- **A cover proxy / caching layer.** The after-cover is fetched directly from CAA by the browser; the before-cover is served uncached (`no-store`) since it's transient parked-album art.
