# Backlog

The living list of known issues, follow-ups, and open questions — so nobody has to re-read
the project (or a whole investigation) to know what still needs attention. One line of
context per item is not enough: each entry says what it is, why it matters, and where the
detail lives.

**How to use:** work the *Next up* section first. When something ships, move its entry to
*Recently shipped* with the PR number. When something new turns up (review finding, incident,
parked idea), add it here in the same commit that discovers it.

_Last groomed: 2026-08-23, with the app-wide origin guard._

## Next up

- Pick from Open bugs / hardening below — security response headers are the standing
  candidate now that the origin guard has shipped (2026-08-23), leaving them the remaining
  half of the security posture. (The phantom-album question that used to sit here is
  RESOLVED — see Open questions and the 2026-08-22 guard entry under Recently shipped. The
  m3u8 export fix and the playlist-name 500 both shipped 2026-08-23.)

## Open bugs / hardening

- **Album art can still be silently diverted.** The collision pre-flight covers item files
  only; `Album.move_art` goes through the same beets `unique_path`, so two album rows
  resolving to one folder with disjoint track names would land `cover.1.jpg` silently.
  One-shot (no churn — the unit is item-settled next run), which is why it was descoped.

- **`static_dir` gate mismatch (residual of the logged-posture fix).**
  `resolve_extra_origins` keys on the truthiness of `MUSICDROP_STATIC_DIR`, but `mount_static`
  (`app/static_files.py:42`) no-ops unless `index.html` exists — so a stale or invalid path
  still yields the production posture (every dev write 403s) with NO SPA served. The silent
  half is closed (the effective posture is logged at startup — see the Host-allowlist entry
  under Recently shipped), but the gate itself may still want to key on the same `index.html`
  check `mount_static` uses, so the posture and the served SPA stay in agreement.

- **OpenAPI under-declares 403 on 61 write routes.** 64 write routes can now return 403 from
  the app-wide guard; `frontend/openapi.json` declares 403 on 3 (counted 2026-08-23). Per
  CLAUDE.md rule 2 the schema is the contract and the TS types are generated from it, so the
  generated client types 403 as impossible where it is reachable. Low impact today (the
  frontend is same-origin); the guard widened this from 6 routes to 64. Either declare a
  global 403 or record the acceptance.

- **Cross-origin no-cors GET side effects are an accepted residual.** `GET
  /api/artists/image` (and its peer cache-fillers), plus the outbound-credential GETs like
  `/api/plex/*`, still fire for a foreign page — GETs are structurally outside an
  unsafe-method guard, so the 2026-08-23 slice's "not in this slice" note stands as an
  accepted risk, not an oversight.

- **No security response headers anywhere.** The backend emits no
  `X-Content-Type-Options`, `X-Frame-Options`, or CSP on any response (verified
  2026-08-09; deliberately left out of the 2026-08-23 origin-guard slice to keep
  it one concern). A reverse proxy is currently the only mitigation, and the
  shipped compose has none. See the memory note `musicdrop-security-posture-gaps`.

- **Bank store sink has the same inf/NaN shape the playlists store just fixed.**
  `app/bank/store.py:174` (`json.dumps(model_dump(mode="json"), ensure_ascii=True)`) writes
  a bare `Infinity` token for a non-finite float — `app/models/bank.py` carries
  `confidence: float | None`. Reachability is low (confidence is set internally by the
  beets matcher, not from a client body), which is why it wasn't fixed in the 2026-08-23
  playlists slice — apply the same `_finite_only` + `allow_nan=False` treatment when the
  bank store is next touched.

- **`get_playlist` propagates `UnicodeDecodeError` on a non-UTF-8 record file** while
  `list_playlists` skips it (its guard is `except (OSError, ValueError)`; get's `read_text`
  sits under `except OSError` only). Pre-existing, unreachable via the store's own sink
  (`ensure_ascii=True` output is pure ASCII) — needs external file corruption. Align the
  two sites' posture when next in the file.

- **Config editor accepts `import.autotag` that MusicDrop now ignores.** `run_import_worker`
  force-enables autotag (with snapshot/restore) because beets swaps out the `user_query`
  stage under `autotag: no` — the only stage that fires `choose_match`, the sole hook
  MusicDrop's outcome tracking hangs on (restore reported `could_not_restore` after a
  SUCCESSFUL import). The setting is still accepted silently; the editor should say it has
  no effect on MusicDrop-driven imports. Note: sweep/bank breakage under `autotag: no` was
  reasoned from the stage list, demonstrated only for restore.


- **Wire-safety net coverage caveats** (by design, recorded so nobody assumes otherwise):
  SSE `/api/events` bypasses the response class (scopes are tag-derived today, never paths);
  any future route-level `response_class=` or hand-built `JSONResponse` bypasses both halves
  of the net.


- **Untested defensive lines** (deep-review survivors, all currently benign — pin when
  touched next): broken-symlink sidecar carry (`sidecars.py` `lexists`), singleton
  crash-path sidecar carry, `edit.py` `_inside_library` guard (pre-existing from main),
  disk-sync emptied-row first-dir-wins and `"."`-fallback, dismiss double-click swallow,
  the aria-disabled-not-disabled focus rule (the "Pagination rule" is convention, not test).
  From the 2026-08-23 m3u8 deep review, on the shared atomic recipe (`atomic.py`): the
  final `chmod` is umask-blind in tests (deleting it survives under the usual umask 022 —
  only visible under 077; a mode test should set the umask itself), the same-directory tmp
  placement is unpinned (a `/tmp`-located tmp survives because pytest's tmp shares the
  device; on a NAS-mounted `.playlists` it would EXDEV every export), the `.m3u8` export's
  0o644 mode is unpinned, and the fsync durability lines rest on review alone (untestable
  without crash injection). From the 2026-08-23 playlist-name review: the store sink's
  `mode="json"` is equivalent-but-forward-fragile (a future `datetime`/`enum`/`Decimal`
  field would make `json.dumps` raise `TypeError` on every mutation — nothing guards it);
  the export's in-window-surrogate name degradation (raw byte → U+FFFD vs #151's direct
  `write_m3u` behavior) is intended but untested; a surrogate-named Plex sync degrades to
  a generic `failed` target state (reasoned from `_safe_reconcile`'s broad except, never
  executed against a real encoder).

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

- **Phantom one-track album row: RESOLVED 2026-08-22.** Root cause diagnosed at the code
  level: beets' import duplicate gate is byte-exact on albumartist+album, and every
  MusicDrop protection was a reactive hook behind it — dead code for a typographic
  variant, so a lone en-dash-titled file minted a sibling row with zero prompts. It WAS
  systematic. Fixed by the import-gate guard (Recently shipped, 2026-08-22). Live
  forensics on the production DB (owner-run, read-only): the phantom and its duplicate
  item were already cleaned up, zero double-counted files, zero orphan singletons —
  library clean; the specific entry path (as-is tags vs an MB variant apply) is
  unrecoverable and both are closed by the guard. Full report: the vault note
  `phantom-album-diagnosis` (authored by pi/qwen, verified line-by-line).
  Follow-ups deliberately NOT built (each needs its own decision):
  the upfront warning endpoint (`find_import_duplicates`) stays byte-exact, so the
  gate can prompt where the pre-import warning showed nothing — align later if wanted;
  and beets' SINGLETON duplicate check structurally excludes album members
  (`NoneQuery("album_id")`), so an explicit as-tracks import of an existing album's
  track can still mint an orphan item silently — the astracks window is deliberate and
  narrow, but unguarded.

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
- Background portrait fills are unbounded in count: a very large roster page schedules one
  task per uncached artist, and they queue behind the 5/s limiter rather than a work queue.
  Bounded in practice by the page size; revisit if a whole-library warm-up is ever added.
- The manual per-source fetch bypasses the negative-cache marker by design (the user asked
  for it), so repeatedly clicking Fetch on a dead source repeats the upstream call. The
  shared limiter paces it; nothing else bounds it.
- `ArtistImageFiller` holds no cap on the in-flight map. A pathological client requesting
  thousands of distinct artist names could grow it; single-user LAN deployment, so noted
  rather than fixed.
- Reset now clears the automatic slot, so the artist shows a monogram until the background
  fill lands. Reset kicks that fill itself (zero grace, so the POST still answers at once)
  and the coalesced `art:changed` drops the portrait in with no reload — ~4s against live
  sources in the wave's browser check — but it IS a visible flash. Expected, and the only
  honest behaviour.
- `ArtistImage` remounts every card on any `art:changed` (global assetVersion key) — near-free
  now that 304s are stat-cheap, but a scoped identity would need the normalized-name mapping.
- Browse-side A-Z index would need a per-filter letter-to-offset endpoint (Artists-only
  shipped in the perf wave).
- **No CI guard that `frontend/openapi.json` matches the live spec.** `grep openapi
  .github/workflows/ci.yml` returns nothing, and `tests/test_config_validate_api.py:60` reads the
  LIVE spec off a TestClient, so a stale tracked file fails nothing. An *added* path is
  self-correcting (frontend code cannot compile against a type that was never generated) but a
  *changed* one is not — a field flipping required→optional keeps generating old TS that still
  compiles, and the contract is silently wrong. This repo has been bitten by exactly that between
  #83 and #111. Shape: a backend pytest comparing PARSED DICTS (not serialized text — a byte
  comparison fails on formatting drift and teaches people to distrust the guard); a missing file
  must FAIL, not skip; the failure message spells out the two-step regen.
- **`download_image` validates only the FIRST and LAST redirect hop, and issues the intermediate
  request anyway.** Measured: `public → 127.0.0.1:9 → public` returns bytes and the internal GET
  happens. Its sibling `fetch_image_bytes` (the user-pasted-URL path) does it correctly with
  `follow_redirects=False` and `assert_public_url` on every hop. So the hardened path is the one
  handling untrusted input and the CDN path is the loose one. Low severity given the CDNs
  involved; the fix is to give `download_image` the same manual hop loop.
- `ArtistImageEditPanel.onPickFile` no longer clears notices and nothing pins it — deleting the
  call passes all 1034 FE tests. Pick a file after a failed fetch or a reset and a stale note
  rides onto the preview screen. Not a regression (the pre-fix inline `fetchImage.reset()` was
  equally unpinned); a third arm on the existing clear-notices test closes it.
- `aside.w-96` on `ArtistAlbumsPage` overflows a 390px viewport by 18px, reproduced with the
  panel closed (`App.tsx:53` gives main `px-6`, leaving ~342px for a 384px rail). Fix is
  `w-full max-w-96 lg:w-96`, not a design change.
- The aria-hidden clickable-name idiom now has TWO instances (`MergePlaylistDialog.tsx`,
  `ImportPlaylistsPage.tsx` since the backlog-minors wave): a third should become a shared
  component. Reviews of it should also check `select-none` isn't suppressing selection of
  user data someone might want to copy — on the import rows the playlist NAME is now
  unselectable, an accepted trade-off of the pattern.
- `SegmentedControl` segments are 28px tall, under the 44px touch-target guidance. Shared
  component; the artist-image wave made it load-bearing on a mobile flow for the first time.
  `py-1` → `py-2` reaches ~36px without touching the visual language; 44px needs a design call.
- Artist-image panel minors, all shipped deliberately: Fetch is `secondary` while the pasted-link
  Set is the only filled control (ranking reads backwards); the URL input's `aria-label` shadows
  its visible label (WCAG 2.5.3, pre-existing — fixing it breaks `getByLabelText` in two files);
  generic `alt="Artist image preview"`; comparing two sources costs a Discard; and a possible
  live-region/focus contention that needs a real screen reader to settle.
- `test_default_settings_disabled_endpoint_404s` proves the 404, not the REASON: with the
  flag mutated to enabled it still passes offline (the inline-grace path also 404s), so
  "no outbound call when off" is asserted nowhere. A bare `@respx.mock` does NOT close
  this — tried and refuted by its own mutant on the backlog-minors wave: the service
  catches source exceptions as transient failures, so an unmocked-call raise is laundered
  into the same 404. A real proof needs a transport spy plus deterministic background-fill
  settling; not worth it until the wiring changes.
- **`tests/test_import_session.py::test_attended_astracks_lands_the_singletons_full_pipeline`
  writes to the developer's PERSONAL beets config dir** (`~/.config/beets/state.pickle`) on every
  full-suite run — bisected as the only offender, and present at least as far back as `643783f`,
  so it predates the path-binding branch. Bounded: only beets' importer scratch state is written,
  the personal `library.db` md5 is unchanged and no `.bak` appears. Same class as the sibling entry
  above and as the 2026-08-15 incident where an agent's unguarded `beet --version` ran two pending
  migrations against that same personal library. Its sibling entry (the two
  `test_artist_image_endpoint.py` lifespan tests that opened the real dev library) was fixed on
  the backlog-minors wave via `_pin_settings_at`, canary-proven with
  `MUSICDROP_BEETS_DIR=/nonexistent`; the durable close for THIS entry and the whole class is
  still an autouse fixture pointing `BEETSDIR` at `tmp_path` for the entire suite.
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

- **Host allowlist (DNS-rebinding guard) — shipped 2026-08-23 (PR # filled in at merge).** All-method
  `HostGuardMiddleware` (outermost): Host / X-Forwarded-Host must be a bare IP literal,
  `localhost`, or a name in `MUSICDROP_ALLOWED_HOSTS` (exact, case/port-insensitive match, no
  wildcards); everything else 400s. Dev posture additionally allows `testserver` (prod-pinned
  not to). Startup now logs the effective security posture (closes the silent-`static_dir`
  minor). Deploy delta: browsing via a DNS name (e.g. the Caddy site) requires
  `MUSICDROP_ALLOWED_HOSTS=<that name>`; by-IP access and the container healthcheck are
  unaffected. Residuals: real auth is still the stronger long-term control (separate item);
  wildcard entries deliberately unsupported until a deployment needs them.
  Spec: `docs/superpowers/specs/2026-08-23-host-guard-design.md`.

- **App-wide origin guard (PR #TBD, 2026-08-23).** Cross-origin browser writes are
  rejected 403 by `OriginGuardMiddleware` on every POST/PUT/PATCH/DELETE — closing
  the 19 CORS-simple routes (config/apply, review-inbox, disk-sync, reorganize,
  playlist sync, ...) that executed with no preflight and no origin check. The
  per-route `verify_upload_origin` dependency is retired; missing-Origin
  (curl/LAN/webhook/healthcheck) still allowed by design — this is a browser-CSRF
  guard, not auth. The `localhost:5173` dev allowance (writes AND CORS read
  access) now applies in dev mode only (`static_dir` empty); prod rejects it.
  **Caveat — this is not "browser CSRF fully closed":** it closes the
  CORS-simple / cross-origin vector, but NOT DNS rebinding (closed separately by
  the Host allowlist — see the Host-allowlist entry above), and not the
  cross-origin no-cors GET side effects (also above).
  Deployment note: a Host-rewriting reverse proxy must FORWARD (set, not
  append) `X-Forwarded-Host`, or every browser write 403s.
  Spec: `docs/superpowers/specs/2026-08-23-origin-guard-design.md`. Note carried
  from the closed CSRF entry: display-name resolution keeps undecodable-named
  trash/inbox folders addressable by a guessable display string for NON-browser
  callers — bounded (ambiguity refuses, no fan-out), inherent to restorability.
  Also closes the deferred "nothing enforces the CSRF policy for the next
  body-less POST" minor: with the guard applied by construction there is no
  per-route dependency left to forget, so the route-enumeration test it asked for
  is moot.

- **2026-08-23 — playlist mutations survive a lone-surrogate NAME** (branch
  `fix/playlist-name-surrogate-500`): a client can deliver a lone surrogate without one
  non-UTF-8 byte (the JSON escape `"\udce9"` parses), and the store sink
  `model_dump_json` raised `PydanticSerializationError` → unhandled 500 on every playlist
  mutation (found by #151's deep review; verified first-hand). Fix mirrors the bank's #147
  single-sink rule: stdlib `json.dumps(model_dump(mode="json"), ensure_ascii=True)` sink +
  `json.loads` → `model_validate` at BOTH read sites (the Rust JSON parser rejects the
  escaped surrogates the sink writes — verified against pydantic 2.13.4). Store lossless,
  wire degrades (`SurrogateSafeJSONResponse` already covered responses); corrupt-file and
  legacy-file behavior pinned unchanged. Plus: the export renders the NAME through
  `wire_safe` — a stored high surrogate (outside surrogateescape's window) would otherwise
  silently kill the playlist's whole `.m3u8` export, the class #151 fixed for paths (lossy
  on the display label, never on the path locators). Plex sync checked: a surrogate name
  becomes a failed target state, never a 500. 10 new tests; 5 mutants killed, each by
  exactly its intended tests.

- **2026-08-23 — `.m3u8` export survives undecodable track paths** (branch
  `fix/m3u8-export-surrogate-paths`): the export sink wrote strict UTF-8, so a track path
  carrying lone surrogates (`os.fsdecode` of undecodable on-disk bytes) raised
  `UnicodeEncodeError`. Correction to this backlog's old claim, established while scoping:
  that never surfaced as a 500 — every export call site goes through the best-effort
  `_export_playlist`, so the real symptom was a **silently stale or missing `.m3u8`** plus a
  warning log. Fix: `write_atomic_bytes` is now the atomic primitive (same recipe, raw
  bytes); `write_atomic_text` delegates with a strict encode — JSON stores and config
  writers keep byte-identical behavior, pinned by a test that the wrapper still raises on
  surrogates; `write_m3u` encodes `surrogateescape` so such paths round-trip to their
  original on-disk bytes (never U+FFFD, which would point the player at a nonexistent
  file). 9 new tests; 3 mutants (strict encode back, `replace`, loosened text wrapper) each
  killed by exactly its intended test.

- **2026-08-22 — import-gate guard: typographic-variant duplicates engage the duplicate
  machinery** (branch `feat/import-dup-guard`): a per-task wrapper over beets'
  `task.find_duplicates`, armed in `choose_match` before beets' `_resolve_duplicates`
  runs, adds a normalized-variant scan (the existing `_fuzzy_part` ladder from
  `duplicates.py` — no third normalization scheme) on top of beets' untouched byte-exact
  pass. An en-dash/case twin now parks the duplicate prompt attended, banks
  `needs_dup_resolution` in a sweep, SKIPs on an undecided directive, and honors all four
  resolution actions — the paths that silently minted the production phantom. beets'
  exclusions mirrored (no-artist as-is; re-import of own files). 14 tests incl. a wiring
  pin that goes through `choose_match` itself (added after a review mutation proved the
  original 13 all armed the guard directly — the install line could be deleted green);
  3/3 mutants killed. Implemented by pi/qwen via the new `pi-delegate` agent, verified
  first-hand.
- **2026-08-22 — playlists list shows Plex sync state at a glance** (branch
  `feat/playlists-plex-status`, owner request): each row carries one badge — Synced, the
  failed sync's own error text, "N not on the Plex copy", or "Out of date; re-sync" —
  computed by the SAME status vocabulary the detail page ships, extracted into
  `plexSyncStatus.tsx` rather than duplicated (detail output pinned byte-identical by its
  suite). The list wire deliberately omits `missing_tracks`, so a summary partial never
  says "re-sync to see which" (false there — opening the playlist shows the misses) and
  never splits absent-vs-duplicate (unknowable on that wire). Multi-target aggregation is
  severity-first: the worst target wins the badge, a lone problem keeps its own label, so
  a failed fan-out target can never hide behind a healthy admin copy. Rows with no Plex
  state stay quiet. Six TDD tests, three mutants killed, live-browser-verified across all
  five states incl. long-name truncation.
- **2026-08-22 — backlog minors wave** (branch `fix/backlog-minors-wave`): the duplicated
  stat-ETag helpers are one shared `app/etag.py` (with the thumb `-t` marker splice pulled in
  beside it, so the quoted tag format is one module's internal contract); `AlreadyInLibrary`
  finally has a test file, pinning the `?size=thumb` cover URL, the tracklist fallbacks and
  structure, and the View links (both key mutants killed); the two
  `test_artist_image_endpoint.py` lifespan tests no longer open the real dev library
  (canary-proven both directions); and the Plex import rows' playlist NAME is a real click
  target (merge-dialog pattern, three regression tests, live-browser-verified with
  fixture interception). Also: **ENAMETOOLONG no longer 500s trash/inbox endpoints** — new
  `app/fsutil.py` guarded predicates (only errno 36 reads as absent; every other OSError
  still raises) at the two request-reachable sites, 10 tests incl. re-raise pins for
  EACCES/ESTALE. And **bank rows with non-UTF-8 folder names now persist** — the store sink
  moved from pydantic's Rust serializer (which refuses lone surrogates) to stdlib `json` with
  `ensure_ascii=True`, the one path that round-trips them losslessly, so `os.fsencode`
  recovers the exact on-disk bytes; legacy rows still load. First wave implemented via
  pi/qwen delegation under Claude review.
- **2026-08-15 — the production `library.db` absolute-path repair, both halves in one window**
  (PR #134 `e9c940c` → v0.34.2, tag verified; the DB half run by hand on TrueNAS the same
  evening). The import worker now binds `lib.music_dir_context()` around its whole body — the
  one function both callers pass through, so Trash restore is covered too — and the one-shot
  beets migration converted the rows: items 22,100 absolute → 0 (dry run predicted 22,100
  exactly), the 123 mixed albums cleared, replacement lookup recovered. The albums half
  legitimately printed no `Migrating` banner (artpaths were already relative);
  `docs/import-path-repair.md` now carries the as-run outcome and that per-table nuance, and
  stays as the runbook.
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
