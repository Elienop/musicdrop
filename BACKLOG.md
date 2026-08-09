# Backlog

The living list of known issues, follow-ups, and open questions — so nobody has to re-read
the project (or a whole investigation) to know what still needs attention. One line of
context per item is not enough: each entry says what it is, why it matters, and where the
detail lives.

**How to use:** work the *Next up* section first. When something ships, move its entry to
*Recently shipped* with the PR number. When something new turns up (review finding, incident,
parked idea), add it here in the same commit that discovers it.

_Last groomed: 2026-08-02, after the reorganize collision wave._

## Next up

_(empty — pick the next slice from Open bugs / hardening or Open questions below. The
phantom-album-row origin question is the one with a deadline of sorts: it matters before the
next bulk import.)_

## Open bugs / hardening

- **Album art can still be silently diverted.** The collision pre-flight covers item files
  only; `Album.move_art` goes through the same beets `unique_path`, so two album rows
  resolving to one folder with disjoint track names would land `cover.1.jpg` silently.
  One-shot (no churn — the unit is item-settled next run), which is why it was descoped.

- **CSRF/Origin posture on state-changing POSTs.** `/api/reorganize` (starts a library-wide
  move), `/stop`, and `/dismiss` are all CORS-simple POSTs with no origin check
  (`verify_upload_origin` covers multipart uploads only). Harden all three together or
  accept the posture deliberately — do not fix one route in isolation. 2026-08-02 addendum
  from the wire-safety audit: display-name resolution makes undecodable-named trash/inbox
  folders addressable by a *guessable* display string (previously unaddressable over HTTP at
  all — Starlette percent-decodes queries with `errors="replace"`). Bounded (ambiguity
  refuses, no fan-out), inherent to making such folders restorable; weigh it when the origin
  decision is made.

- **`.m3u8` playlist export still violates the no-500 invariant.** `write_atomic_text` opens
  `encoding="utf-8"` strict, so a playlist rewrite 500s on an undecodable track path. The
  right fix is writing raw bytes (`surrogateescape`), NOT a U+FFFD placeholder — a
  placeholder inside an `.m3u8` silently points the player at a nonexistent file — and the
  primitive is shared with the JSON stores, so it needs its own slice.

- **Bank rows cannot be persisted when the source folder name is not valid UTF-8** —
  `store.py` `model_dump_json` raises `PydanticSerializationError` (different sink from the
  wire; the response-class net does not cover files on disk). Pre-existing; surfaces only
  when an undecodable folder enters the bank.

- **MusicDrop-driven imports store ABSOLUTE item paths.** The import worker never binds
  `lib.music_dir_context()` around `session.run()` (only `_trash_replaced_albums` does), so
  imported rows store absolute paths while beets' `relative_path` migration expects
  music-dir-relative ones — likely defeats library portability for every album imported
  through MusicDrop. Flagged by the 2026-08-02 restore debugging; consequences not yet
  chased.

- **Config editor accepts `import.autotag` that MusicDrop now ignores.** `run_import_worker`
  force-enables autotag (with snapshot/restore) because beets swaps out the `user_query`
  stage under `autotag: no` — the only stage that fires `choose_match`, the sole hook
  MusicDrop's outcome tracking hangs on (restore reported `could_not_restore` after a
  SUCCESSFUL import). The setting is still accepted silently; the editor should say it has
  no effect on MusicDrop-driven imports. Note: sweep/bank breakage under `autotag: no` was
  reasoned from the stage list, demonstrated only for restore.

- **ENAMETOOLONG is a 500 on trash/inbox endpoints** (pre-existing): `Path.exists()` only
  swallows ENOENT/ENOTDIR/EBADF/ELOOP, so a >255-byte name component re-raises where a 404
  was intended. Needs guards at the `exists()` call sites.

- **Wire-safety net coverage caveats** (by design, recorded so nobody assumes otherwise):
  SSE `/api/events` bypasses the response class (scopes are tag-derived today, never paths);
  any future route-level `response_class=` or hand-built `JSONResponse` bypasses both halves
  of the net.


- **Untested defensive lines** (deep-review survivors, all currently benign — pin when
  touched next): broken-symlink sidecar carry (`sidecars.py` `lexists`), singleton
  crash-path sidecar carry, `edit.py` `_inside_library` guard (pre-existing from main),
  disk-sync emptied-row first-dir-wins and `"."`-fallback, dismiss double-click swallow,
  the aria-disabled-not-disabled focus rule (the "Pagination rule" is convention, not test).

- **Disk-sync emptied-row path shows the first item's folder,** not the album root — a
  multi-disc emptied album reads `Artist/Album/CD1`. Still separates label twins (the
  feature's purpose); commonpath would be nicer.

- **Artist-image cache: after a broken cache dir is repaired, affected artists never return
  to disk.** The in-memory stand-in (`ArtistImageCache._MemoryFallback`, added in the
  perf/images wave) is dropped by `store_positive`/`store_negative` on a write that succeeds —
  but a memory HIT satisfies the request without ever calling either, so for those keys the
  write never happens and the discard never fires. Measured: 5 decode+resizes for 5
  post-repair requests vs 1 for a healthy control, with the artist's slot still absent from
  disk. Bounded (the map is capped) and off the event loop, so this is a silent CPU/latency
  regression, not a correctness one — but it persists for the process lifetime and there is no
  log after repair, because the writes stopped failing. Note that `cache.py`'s
  `_or_remember` docstring ("hands authority back to disk without a restart") is true only for
  keys that get written again, which these never are. **Needs a design call before anyone
  codes it:** re-write to disk on a memory hit (simple, but puts a write on the read path), or
  periodically re-probe the dir and flush (more moving parts, keeps reads read-only). Do not
  assume either.

## Open questions

- **Where did the phantom one-track album row come from?** The 2026-08-01 incident's origin:
  an import created a second album row ("Greatest Hits – Chapter One", en dash) holding one
  duplicate track. Leading suspicion: duplicate-merge + as-is import path. If it is
  systematic, big imports will keep minting phantoms; worth a look at the import/merge flow
  before the next bulk import. Detail: memory `reorganize-collision-churn`.

- **Hardlink alias of a vacating unit-mate** may refuse instead of settle (the alias keeps
  the inode alive across the move, so the next sweep's pre-flight still sees it). Reasoned,
  not reproduced; end state is a refusal with an honest message, not damage.

## Deferred minors (cosmetic / self-healing — carried from earlier waves)

- Artwork degrade logging: three of the six log sites are pinned by no test — mutations that
  silence `cover_thumbs.py`'s thumb-cache-unwritable line, `cache.py`'s
  `mime-sidecar-unsendable` line and `cache.py`'s thumb-cache-unwritable line all SURVIVE.
  The `mime-sidecar-unsendable` one matters most: it REVERSES an earlier decision in the same
  wave that was explicitly argued in a comment ("deliberately NOT logged here", to avoid
  per-request spam) and later made safe by the throttle. Nothing pins the reversal, so a
  future reader who finds the old reasoning can delete the line and the suite stays green.
- `_MemoryFallback`: the 512-entry cap, the byte decrement in `_discard_locked`, and the
  refusal of a single image larger than the whole budget are all untested (mutations survive;
  only the byte budget's eviction is pinned). The entry cap is the ONLY bound on negatives,
  since they are not charged bytes — so an unwritable cache dir plus a large library can grow
  the map by one entry per no-match artist until the cap catches it.
- Artwork degrade logging discards fault SCALE. The throttle keys on the condition, so one
  corrupt artist out of 5,000 and "WebP support is gone across all 5,000" produce
  indistinguishable output, and a systemic fault arriving behind a single bad file inside the
  same 60s window is silenced entirely. Cheap fix when touched: carry a suppressed-since count
  in the message.
- `"cache-write"` is one throttle key across four sites in two different cache directories
  (artist images and cover thumbs), so for the first 60s of a fault the emitted line can name
  the wrong subsystem. There is also no "recovered" line anywhere, which compounds the
  never-returns-to-disk item above: nothing marks the end of a degraded period.
- Warm Browse/list pages still do a per-row `lib.get_album` (~2 queries/row); batch the
  id-IN load or widen `BrowseRow` instead.
- No next-page prefetch on Browse/Artists — a page flip waits a round trip
  (`placeholderData` already prevents blanking, so this is latency, not breakage).
- Cold artist-image pages resolve inside the request under the 5/s source limiter — first-ever
  view of an uncached page takes ~10s to fill. Consider respond-fast + background fill; pairs
  with the planned per-artist re-fetch feature.
- `ArtistImage` remounts every card on any `art:changed` (global assetVersion key) — near-free
  now that 304s are stat-cheap, but a scoped identity would need the normalized-name mapping.
- Browse-side A-Z index would need a per-filter letter-to-offset endpoint (Artists-only
  shipped in the perf wave).
- **`tests/test_artist_image_endpoint.py` opens the REAL dev library on every suite run.** It
  uses `with TestClient(app)` at lines 257 and 298 and contains zero `beets_dir` references, so
  the lifespan runs `setup_beets` against `MUSICDROP_BEETS_DIR` from `backend/.env` — the live
  `data/beets`. Nothing autouse guards this: the hermetic `beets_library` fixture
  (`tests/conftest.py:254`) is opt-in, and 14 other test files do patch `settings.beets_dir`
  while this one does not. No debris today (beets 2.13 writes `library.db-before-*.bak` only
  when a migration actually runs, and the dev DB is current), but it holds a SQLite connection
  to the real corpus and would back it up on the next beets schema bump. Fix is one line:
  `monkeypatch.setattr(settings, "beets_dir", str(tmp_path))` plus a tmp `config.yaml` +
  `music/` dir, per the `test_slskd_webhook.py` pattern.
- `_stat_tag` (artwork) duplicates `library.py`'s `_stat_etag` (Path vs str param) — polish
  only, same behavior.
- Stat-then-read ETag race on the artwork cache (self-healing, mirrors the covers precedent);
  its integration test only exercises the hash-fallback branch.
- Thumb edge-case paths (animated/palette/CMYK/tiny source images) were verified by reviewer
  probes but are unpinned by tests; the alpha test only checks image mode.
- `.thumb.src`/`.thumb.bin` pair is not atomic as a unit (a `clear_override` tag-revisit
  corner); served-tag vs. served-bytes TOCTOU is inherited from the cover cache class (fix:
  `get_thumb` should return its src_tag, which also drops a stat); the 304-path + off-loop
  constraints are unpinned by tests.
- `get_artist_image_cache`'s sibling has no lazy fallback (same latent isolated-run fragility
  the cover dep fix addressed); `cover_client` has a pre-existing beets_library state leak; no
  eviction of cover thumbs on album delete (parity with the artist cache is missing).
- `AlreadyInLibrary` thumb URL has no test file.
- Pagination: numbered-window `aria-label="Page N"` vs. the visible "N" is a WCAG 2.5.3
  label-in-name partial mismatch; the icon-sm caret width is tight for 3-digit page numbers.
- Pagination: a component docstring is stale re click-to-edit; there's a minor spinner style
  delta from the original design. (The busy-state-not-in-the-accessible-name and
  untested-empty-commit items were fixed in v0.33.1 / PR #130 — the competing `aria-label` is
  gone so the sr-only child wins, and the empty/clamped commit paths are pinned in
  `Pagination.test.tsx`.)
- Artists A-Z index: the active-letter tint omits `SegmentedControl`'s `shadow-xs`; CJK/
  Cyrillic artist names fall into `#` (documented limitation, not a bug).
- Browse cache rebuild: a transient wrong-row window exists when an album commits
  between the facts snapshot and the albums scan (self-healing via the generation bump);
  `_EMPTY_FACTS` and the format tie-break are untested; one single-implementation guard test's
  name over-claims.
- Genre facet play-order: a NULL-vs-0 disc/track tie can diverge from SQL's NULL-first order
  (narrow edge, untested); `duplicates.py`'s genre consumer isn't directly exercised by the
  consistency test (it shares the call shape with what is tested).
- Genre: legacy flex `genre` (singular) rows are now unreadable post-fix — zero exist in the
  real library, informational only; an `assert isinstance` vanishes under `python -O`; the
  `_collect_facts` docstring says "`_coerce_*` helper" but genres actually go through
  `_genre_values`.
- **Genre facet is PRIMARY-genre-only** (deliberate tradeoff, preserves the counts-sum
  invariant): on the real library 17/36 albums are multi-genre, so tag *order* decides the
  bucket (`Hip Hop\0Gangsta Rap` vs. `Gangsta Rap\0Hip Hop` land in different buckets), and
  selecting "Rock" won't surface an album tagged `["Pop", "Rock"]`. Multi-bucket faceting
  would break the counts-sum invariant — flagged for awareness, not scheduled.
- 413 responses carry no CORS headers (dev-only annoyance).
- PlaylistDetail stale-snapshot move-PUT (self-heals on refetch).
- Remove-to-empty stale focus; playlist-import focus-after-resolve.
- Trash restore leaves `.lrc`/`.txt` sidecars behind in Trash (v1 limitation, noted in #67).
- Trash restore leaves the emptied source folder behind as a 0-track husk row in the listing
  (name-independent; beets moves the files but never prunes the dir — Empty clears it).
- MoveNotice in AlbumEditPanel is legacy hand-rolled banner markup that StatusBanner's own
  docstring claims to generalize (rounded-md/gap-2/size-4 vs canonical rounded-xl/gap-3/
  size-5) — mechanical migration to `<StatusBanner tone="warning">`, flagged 2026-08-02.
- ConflictList and MoveRefusalList scroll containers have no focusable children/tabindex, so
  Safari keyboard users can't scroll them (Chrome/Firefox auto-focus scrollers). Shared
  idiom — fix both together or neither.
- Trash/inbox rows key on the scrubbed display name (`key={album.folder}` /
  `key={item.name}`), so two differently-damaged non-UTF-8 siblings share a React key
  (duplicate-key warning, possible node reuse). Cosmetic — either row's action 409s cleanly.
- `empty_trash_one` resolves the display name outside the swap lock (`restore` resolves
  inside it); the placeholder scandir path widens that pre-existing TOCTOU window slightly.

## Recently shipped

- **2026-08-08 — perf: images, pager, cache wave** (branch `feat/perf-images-pager-cache`):
  artist-image and album-cover 304s now answer from a file stat (no read, no hash, off the
  event loop); new 320px WebP thumb variant (`?size=thumb|full`) — grids/tiles request thumbs,
  the two detail-page heroes deliberately keep full-size art. `Pagination.tsx` gained
  first/last double-caret jumps, a numbered ellipsis window, and a click-to-edit jump-to-page
  input; new A-Z index on the Artists page; roster sort is diacritic-insensitive. `/api/stats`
  now answers from SQL aggregates independent of the browse cache; the browse-cache rebuild is
  one aggregate pass (~4-5 queries, no more per-album `items()`); SSE invalidation bursts
  coalesce (300ms debounce, ~2s max-wait, cancelled on reconnect); SSE-invalidated query
  families get a 5-minute staleTime. Along the way: **I17 (event-loop stall from the
  browse-cache rebuild) is CLOSED as originally stated** — the 2026-08-08 perf investigation
  refuted the loop-stall theory (invalidation is an O(1) generation bump; only the paying
  request stalls), and this wave fixes the real cost, the rebuild's own N+1 query expense.
  Also fixed in-scope: the beets 2.13 genre pipeline was dead end-to-end (beets 2.13 has no
  `genre` field; Item/Album both use `genres`) — the Genre facet, `Album.genre`, and the edit
  panel now read/write `genres`, replacing "Unknown" for the entire library with real genre
  values (see the multi-genre primary-bucket tradeoff noted under Deferred minors).
- **2026-08-02 — wire-safety + edit-preview wave** (branch `fix/utf8-paths-and-edit-preview`):
  non-UTF-8 paths no longer 500 any JSON endpoint — sink-level scrub
  (`SurrogateSafeJSONResponse` + scrubbed HTTPException/RequestValidationError handlers),
  with Trash/inbox display-name keys resolved back to real on-disk entries (409 on ambiguous
  display twins, incl. a literal-U+FFFD folder shadowing a damaged one; NUL-safe). Found and
  fixed along the way: `run_import_worker` now force-pins `import.autotag` on — under
  `autotag: no` beets drops the only stage that fires `choose_match`, so every import surface
  reported nothing-happened after a successful import (restore said `could_not_restore` while
  the files landed). And the edit preview now mirrors apply's collision pre-flight: new
  `AlbumEditPreview.move_refusals` contract field, refusals rendered in the ConflictList
  idiom with per-track detail plus the partial-application consequence stated ("tags still
  write; files keep their names"). Detail: memory `wire-safety-serialization-gotchas`.
- **2026-08-02 — reorganize collision wave** (branch `fix/reorganize-collision-wave`):
  proven `.1↔.2` rename churn on destination collisions, killed by a pre-flight that refuses
  before moving; sidecars follow audio through every reorganize/edit move (they used to be
  swept to Trash while the job reported success); tag-edit moves verified and refused the
  same way; failure list dismissible + stamped with its run time (was: unclearable until
  container restart); disk-sync emptied-albums rows carry track count + folder (a 1-track
  phantom no longer reads as your 17-track album). Full investigation ledger: memory
  `reorganize-collision-churn`. Post-deploy note: restore stranded lyrics from Trash or run
  one lyrics backfill.
- Earlier history lives in the PR log (#1–#123) and the memory index — this file starts here.
