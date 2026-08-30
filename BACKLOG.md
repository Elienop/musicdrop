# Backlog

The living list of known issues, follow-ups, and open questions — so nobody has to re-read
the project (or a whole investigation) to know what still needs attention. One line of
context per item is not enough: each entry says what it is, why it matters, and where the
detail lives.

**How to use:** work the *Next up* section first. Everything under *Open bugs / hardening* is
work someone could pick up; anything already decided against lives under *Accepted residuals
and deliberate decisions* instead, so it never reads as an open task. When something ships,
move its entry to *Recently shipped* with the PR number. When something new turns up (review
finding, incident, parked idea), add it here in the same commit that discovers it.

_Last groomed: 2026-08-28, from the full-board re-derivation: every open bug re-verified
against the code at `1ae41ff` by an independent pair (re-deriver + adversarial skeptic), plus
four sweeps for work recorded nowhere — source markers, contract drift, deferred-minors
triage, and a data-loss lens. New findings open the Open-bugs section; every pre-existing
entry carries a dated correction block where the pass changed it._

## Next up

1. **The data-safety slice** — the 2026-08-28 data-loss findings that destroy user files or
   rows with no confirmation and no in-app recovery: the lyrics-backfill sidecar deletion
   (critical, first entry below), the unmounted-share ghost delete, and the Trash rows
   Restore can never restore (with the orphan-sweep feeder that fills them). All in the
   beets adapter.
2. ~~**The small-fix slice**~~ — **SHIPPED as #191** (2026-08-28), grown mid-slice by the
   owner's advisory-channel pick and the NaN clamp: read postures (purge-unless-applying
   decided), `static_dir` warning, disk-sync album root, import restore leak + singletons
   hoist, config advisories end to end. The four vacuous pins had already landed in #190;
   still open from this item's old wording: the mypy exemption-list trim (its own entry
   below).
3. ~~**The bank re-run-vs-replay**~~ — design settled 2026-08-29 (vault decisions 24) and
   **BOTH halves shipped in #196**: the backend
   pin (see its struck entry under Open bugs) AND the honest unpinned-row notes on both
   bank decision screens — the duplicate screen's note names the on-screen Rescan remedy,
   the candidate screen's keys on the *selected* option. The `.m3u8` half of this item
   shipped as #195 — see the struck staleness entry under Open bugs.
4. **Real authentication** — the standing long-term security item, shaped 2026-08-28: 112
   operations (66 state-changing), all reachable unauthenticated by anything that can open
   TCP to port 3030 (compose publishes `0.0.0.0`); two unauthenticated calls end the
   library (`DELETE /api/artists` per artist, then `DELETE /api/trash/all`); and the
   exposure is dual-path (by-IP plain HTTP *and* via Caddy), so proxy-level auth alone
   cannot cover the by-IP path the owner actively uses. The existing guards are a coherent
   browser-CSRF + DNS-rebinding pair and none of them is authentication — their own
   docstrings say so. Full posture analysis and option comparison: the vault note
   `musicdrop-auth-posture`.
   **Slice 1 — the backend session gate — shipped in #199:** every
   `/api/*` route plus the docs surface (`/docs`, `/redoc`, `/openapi.json`) now requires
   an HMAC-signed session cookie minted by `POST /api/auth/login` against the scrypt hash
   in `MUSICDROP_PASSWORD_HASH`; exempt exact paths: `/api/health`, `/api/slskd/webhook`
   (its own fail-closed secret), `/api/auth/login`, `/api/auth/status`. The gate matches
   both the raw and root-path-stripped scope path (fail-closed OR), the signing key is
   bound to the password hash so rotating it evicts every session, and the whole test
   suite exercises the live gate via a conftest-minted cookie.
   **Slice 2 — the login UI — shipped in #200** (`0eca9c2` = v0.47.0): `/login` outside the
   shell, a `RequireAuth` guard, transport 401 handling across both the openapi-fetch client
   and `apiFetch`, sign out; plus the scheme-conditional `Secure` cookie and `version` moved
   onto a gated `GET /api/version`. **The do-not-deploy hold is lifted** — v0.47.0 is the
   first deployable release of this work.
   **Slice 3 — re-open the "no auth" justifications — shipped in (PR # filled in at
   merge).** No behaviour change: the OpenAPI dump regenerates byte-identical, and an AST
   diff against `0eca9c2` with docstrings stripped is IDENTICAL on all five app files, so
   nothing executable moved. The one addition is a test —
   `test_assert_public_url_rejects_every_normal_integration_base_url` — which makes the
   anti-hardening claim executable; mutation-tested by neutering the guard's loopback and
   private terms, which reddens it. Re-derived 2026-08-30
   by three investigators under nine adversarial verification lenses, zero refutations, and
   the result inverts the framing this entry used to carry: the justifications were **not**
   made stale by auth, they were **false when written** and auth has just made them true.
   Each claimed the value was "admin-controlled (only the single self-hosted owner can set
   it)" while any Origin-less `curl` on the LAN could write it — roughly twelve weeks for the
   Plex one (written 2026-06-07 in #24; the gate shipped 2026-08-30). So the work is
   replacing an assumption with a named enforcement mechanism, not conceding anything new.
   It is also **five** sites, not three: the two `base_url` concessions and the `/import`
   containment, plus `security_headers.py`'s CORP bullet (which cites "with no
   authentication" outright and is the one genuinely stale reason) and
   `artwork/download.py`'s DNS-rebind scope-out (which auth *strengthens*). Two hypotheses
   were tested and **refuted**: the gate-exempt slskd webhook does not reach arbitrary-path
   import (doubly contained by `contain(..., strict=True)` in the handler and again in the
   queue, and it forces its own `operation="move"`), and it never reads `base_url` at all.

The 40 banked #143 Plex review Minors stay fully adjudicated (2026-08-25, every item
re-verified against v0.44.0): 12 shipped as the triage fix slice (see Recently shipped), 12
recorded below, 3 accepted as deliberate, 3 were already fixed. Of the 12 recorded, the
three that sat under Open bugs shipped in #184; the nine under Deferred minors remain open.
Dispositions with per-item evidence: the vault note `plex-143-review-minors`.

## Open bugs / hardening

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
  **Re-fired 2026-08-30, and the new fact is that the workaround is unenforceable.** The
  standing mitigation is a human rule — "this file is CI-only, never run it locally" — and
  nothing in the repo implements it: `uv run pytest` from `backend/`, which is the exact
  Dev command `CLAUDE.md` documents, collects all 91 of this file's tests and runs them
  (2806 collected with, 2715 without). So the rule is violated by following the project's
  own instructions, and it fails **silently** — the suite is green and nothing names the
  file. Measured blast radius this time, matching this entry's bound exactly: personal
  `library.db` mtime unchanged, no new `.bak`, only `state.pickle` rewritten — its
  `taghistory` set replaced with the single path
  `/tmp/pytest-of-<user>/pytest-N/test_sweep_mode_stores_relativ0/inbox/Artist - Album`
  and `tagprogress` emptied. Consequence for the developer: a personal
  `beet import --incremental` re-offers folders it had already marked done, and an
  interrupted import loses its resume point. Nothing is deleted or moved. The fix is
  unchanged and now has a second reason to be a fixture rather than a rule: a rule that
  only lives in a session's memory is one an agent, a `--` invocation, or a fresh
  contributor breaks on their first test run.

- **The slskd webhook secret has no minimum length, no rate limit and no lockout — and it
  is the ONLY thing in front of a gate-exempt import trigger (2026-08-30, found while
  re-deriving the auth justifications for slice 3).** `POST /api/slskd/webhook` is one of
  four exact paths exempt from the session gate, precisely *because* it carries its own
  secret (`app/api/slskd.py::require_webhook_secret`) — so that secret now does a job the
  session cookie does everywhere else, and it is held to a much weaker standard. Three
  gaps, all verified at `0eca9c2`: (1) **no minimum length.** `SlskdSettingsUpdate.
  webhook_secret` (`app/models/slskd.py:38`) is a bare `str | None` with no
  `StringConstraints`, and `app/slskd/config.py::SlskdConfig.webhook_secret` a bare
  `str` — a one-character secret saves cleanly, and the UI gives no hint that it
  shouldn't. (2) **No attempt throttling anywhere on the path.** No *inbound* rate limiter
  exists in this app at all: `TokenBucketLimiter` paces outbound artwork fetches, and auth's
  `anyio.CapacityLimiter(1)` serialises *scrypt* verification and so only incidentally
  paces login guessing. (Stated as a predicate rather than an enumeration on purpose — a
  review of this entry named a "third limiter", `RateLimitAdapter`, which on inspection is
  **beets'** own `TimeoutAndRetrySession` described in a docstring at
  `app/beets/research.py`, not a MusicDrop limiter at all.) The webhook's
  `hmac.compare_digest` is microseconds and
  unserialised, so guesses run at connection speed. (3) **No lockout or alerting** — an
  unbounded run of 401s is indistinguishable from silence. The comparison is unflattering
  in exactly the direction that matters: the password behind the session gate is scrypt-
  hashed and rate-paced, while the secret that bypasses that gate is compared in
  constant time and otherwise unprotected. Fix shape (ordered by value): a `min_length`
  on both models plus a one-time migration warning for an existing short secret; then a
  per-IP failure counter with a backoff, on this path only. The comparison itself is
  already correct — `compare_digest` on bytes, with the surrogateescape and
  dependency-ordering reasoning documented in the docstring; do not touch that half.
  **Not a regression and not urgent** — it predates auth and the webhook still fails
  closed when no secret is set. It is filed now because slice 1 changed its *role*: it
  used to be one lock among many equals, and is now the single exception in a locked
  house.

- **`POST /api/plex/test` reports success for a server that sent nothing, and 500s on one
  that sent the wrong thing (2026-08-30, found while re-deriving the auth justifications
  for slice 3).** `app/plex/service.py:30` promises *"never raises"* and returns a friendly
  `PlexConnection(ok=False, error=...)` for the Settings panel to render. It catches
  `PlexApiException` and `RequestException` — neither of which covers what a **non-Plex**
  HTTP server produces, and pointing `base_url` at the wrong port is the single most
  likely operator mistake this button exists to catch. Measured 2026-08-30 against a
  throwaway `http.server` on 127.0.0.1, all three answering `200`:
  - `200` + an **empty body** → `ok=True, server_name="Plex"`. A green *Connected*
    against a server that sent zero bytes. Mechanism: `plexapi.utils.parseXMLString`
    returns `None` for a blank body (`utils.py:838`), `PlexObject.__init__` then skips
    `_loadData` entirely (`base.py:119-120`), so no attribute is ever set and
    `getattr(server, "friendlyName", "") or "Plex"` manufactures the name from the
    fallback. Nothing raises, so nothing is caught.
  - `200` + **any well-formed XML that is not a Plex response** → also
    `ok=True, server_name="Plex"`, by the same route: it parses, but carries no
    `friendlyName`, so the fallback fires. Measured for `<html><body>hi</body></html>`
    and `<div>x</div>`, both of which are valid XML. This is the widest arm and the
    empty-body framing above understates it — **the rule is that any 200 whose body
    parses as XML without a `friendlyName` reports a successful connection**.
  - `200` + **malformed HTML** (e.g. an unclosed `<br>`) → escapes as
    `xml.etree.ElementTree.ParseError: mismatched tag`.
  - `200` + **JSON** → escapes as `ParseError: not well-formed (invalid token)`.

  `ParseError` subclasses `SyntaxError`, so it is outside both `except` arms and reaches
  the client as a 500 rather than the typed body the panel branches on. Fix shape: catch
  `ElementTree.ParseError` (or the broader "anything the seam can throw") in `_friendly`
  and, separately, refuse to report `ok=True` when the server produced no parseable
  identity — check for a real `friendlyName`/`machineIdentifier` instead of falling back
  to the literal `"Plex"`. **The false-success arms are the more serious half**: an error
  is visible, a green *Connected* against the wrong server is not, and it is the arm that
  fires for the most likely mistake (a wrong port on a box that runs other web services).
  The slskd twin is already correct for comparison —
  `app/slskd/client.py` catches `ValueError` around `response.json()` and degrades to the
  raw text.

- **`server.url(key, includeToken=True)` puts the Plex admin token in a query string
  (`app/plex/playlists_pull.py:129`, 2026-08-30).** The custom-poster fetch bypasses
  plexapi's `PlexServer._headers()` — which is where the token normally travels, as
  `X-Plex-Token:` — and instead calls `server._session.get()` on a URL that
  `PlexServer.url` has appended `?X-Plex-Token=<token>` to (`plexapi/server.py:883-886`).
  It is not gratuitous: the raw `_session` carries no auth of its own, so *something* had
  to supply it, and `includeToken=True` is the shortest way. The cost is that the token
  lands in the Plex server's own access log, in any intermediary's, and in a URL that can
  be shoulder-read from a traceback — plexapi's own `logfilter.add_secret` scrubs it from
  *plexapi's* logging and cannot reach any of those. This is the Plex **admin** token,
  which is full account control, not a scoped read key. Fix shape: pass
  `headers=server._headers()` to `_session.get` and drop `includeToken`, keeping the
  header path the rest of the module already uses. Low severity — the log it leaks into
  belongs to the server that already holds the token — but it is a one-line change with
  no behavioural risk, so the ratio is good.

- **`POST /import` gives a signed-in owner two footguns with no confirmation and no way
  out (2026-08-30).** Distinct from the containment ruling recorded under *Accepted
  residuals* — that ruling is about an **attacker's** marginal capability and it stands.
  This is about the **owner's** own typo, which it never covered. (1) `{"path": "/"}` is
  accepted and starts a beets autotag walk of the whole container filesystem. The only
  path validation on the way in is `BeetsImportRunner.validate`
  (`app/import_jobs/runner.py:99`), and it checks exactly one thing — an *explicit* copy
  of an in-library source; a root path in the default mode passes it untouched, as does
  the model (`app/models/import_api.py::StartImportRequest.path` is constrained to non-empty
  after stripping, and nothing more).
  The single-slot policy then works against the operator rather than for them:
  `ensure_import_can_start` (`app/api/import_.py:86`) plus the registry's
  `RuntimeError -> 409` mean this one job holds the only import slot, so every later
  import — plus the lyrics, artist-art, reorganize and disk-sync backfills that share the
  gate — returns 409 until the walk finishes, and the screen offers no confirmation for a
  path far outside the usual `/downloads`. What the walk *does* to the files follows the
  user's beets config, not MusicDrop: `run_import_worker`'s `move=None` "touches nothing"
  for a manual import, and the shipped starter config is `copy: yes` / `move: no`
  (`app/beets/config.starter.yaml:20-21`), so a stock install fills the library disk with
  a copy of every music file it can reach — while an operator who set `move: yes` gets the
  destructive version of the same typo. (2) There is no cheap pre-flight: nothing reports
  how many candidate folders a path contains before the slot is committed to it. Fix shape
  (smallest first): refuse — or interstitially confirm — a path that is a filesystem root
  or an ancestor of the configured music library; then a `dry_run` probe returning a
  candidate count. Explicitly **not** an allowlist: that is the exact remedy the
  2026-07-07 ruling rejected as workflow friction, and the reasoning it gave for ad-hoc
  `/downloads` imports has not changed.

- **The frontend has no linter, so the Sonar "lock-on-clear" rule cannot hold there — and
  three cleared families have now measurably regrown (2026-08-30, found while clearing auth
  slice 2's Sonar violations).** The owner's standing instruction is that the PR driving a
  family to zero also enables its lint twin, so CI pins it (recorded under the Sonar
  programme below, and in auto-memory `sonar-lessons-standing`). The backend honours this
  through ruff `select` — PT018, PT012, PT001, S324. **The frontend never could:** there is
  no `eslint.config.*`, no eslint dependency, and no `lint` script in
  `frontend/package.json`; the only related package is `vitest-sonar-reporter`. The
  consequence is no longer hypothetical. Auth slice 2 reintroduced, in new code, three
  families this programme had already driven to zero: `S9020` (`waitFor` + `getBy` instead
  of `findBy`, cleared in Wave 2) ×2, `S6819` (`role="status"` instead of `<output>`,
  cleared in Wave 4) ×1, and `S1874` (a deprecated type) ×2 — seven violations total, all
  caught only by the server-side scan, after the code was written and reviewed. Fix shape:
  its own PR (the programme already scoped it that way — do not bolt it onto a feature
  branch) adding ESLint with `eslint-plugin-sonarjs`, enabling **only** rules whose families
  are already at zero, and mutation-testing each enabled rule by reintroducing the smell and
  confirming the lint reddens. Until then the server scan is the only net for every frontend
  family, which means every regrowth costs a scan-fix-rescan cycle per PR rather than being
  caught in the editor.

- ~~**`GET /api/health` publishes the exact backend version to unauthenticated callers
  (2026-08-30 security audit of auth slice 1, finding L6 — deferred to auth slice 2).**~~
  — **FIXED in #200, 2026-08-30 (auth slice 2):** `version` moved to
  a gated `GET /api/version` and dropped from the health payload, which is now `{"status":
  "ok"}` alone. The sidebar reads the new endpoint, so the string only renders for a
  signed-in caller; the Dockerfile HEALTHCHECK reads only `.status` and was unaffected, as
  predicted. An anonymous scanner now learns that something answers, not which release.

- ~~**Three internal comments drifted from enforced behavior (2026-08-29 README audit;
  comment-only, fold into the next code PR — a docs-only PR can't carry them without
  cutting a release).**~~ — **FIXED in #195,
  2026-08-29:** all four sites corrected (Plex section semantics ×2, lyrics pacing, panel
  nav path). (1) `backend/app/config.py:105-106` and `backend/app/plex/config.py:29`
  both say an empty `MUSICDROP_PLEX_LIBRARY_SECTION` means "the first artist section";
  the enforced behavior (`backend/app/plex/client.py:39-42`, mirrored in
  `PlexSettingsPanel.tsx`) is: sole artist section, else refuse with "Multiple Plex music
  libraries found." (2) `frontend/src/components/settings/PlexSettingsPanel.tsx:74`'s doc
  comment still says "Settings → Plex"; the panel renders only on Integrations.
  (3) `backend/app/config.py:35-36`'s comment on the lyrics delay says beets adds no
  pacing; beets 2.13 rate-limits LRCLib itself (0.25 s/request with 429 backoff —
  `beetsplug/lyrics.py:176-178`), and the runner docstring
  (`backend/app/lyrics_jobs/runner.py:6-7`) already says so.

- ~~**Lyrics backfill silently deletes and overwrites the user's own `.lrc`/`.txt`
  sidecars**~~ — **FIXED in #189.** The file layer is fill-gaps-only; skip paths touch no
  files; the sole deletion is content-proven (a sidecar whose entire body is beets'
  `[Instrumental]` marker, timestamped legacy form included) and runs BOTH ways — an
  instrumental verdict removes marker files, a found verdict treats an all-marker set as
  absent so fetched lyrics reach Plex. Read AND write paths are guarded against non-regular
  files (a planted FIFO used to park the single-slot backfill worker until restart).
  `remove_lyric_sidecars` was renamed `remove_instrumental_marker_sidecars` — it can no
  longer blanket-delete. Residuals, all recorded 2026-08-28: (a) a legacy track with a
  NON-EMPTY `item.lyrics` and a marker-only sidecar returns `skipped_existing` before the
  writer runs, so its marker survives — closing it costs content-reads on every
  sidecar-bearing track per sweep; a force re-fetch clears it (accepted); (b) stale markers
  on already-flagged instrumental tracks are no longer swept (skip paths touch nothing) —
  force clears; (c) a user's own minimal `[Instrumental]` file is indistinguishable from
  the legacy artifact and is deleted by design — content is the only authorship proxy;
  (d) a dangling symlink squatting the atomic-write tmp path is a safe, logged, repeated
  refusal (widening cleanup to `lexists` was deliberately not done inside a data-safety
  fix).

- ~~**Delete album/artist with the music share unmounted silently drops the beets rows
  while the UI says "recoverable in Trash"**~~ — **FIXED in #189.** `require_library_root`
  (missing/empty/unreadable ⇒ unavailable, unreadable named distinctly) now lives in the
  base adapter and guards both trash primitives BEFORE the first mutation; both delete
  routes declare 503; the artist fan-out checks the root before its transaction and a
  mid-flight drop reports honest partial progress ("n of m") through the 500 arm; and a
  post-condition in `trash_album` verifies the move actually happened before rows drop —
  re-asking the root so a share vanishing between check and move is a 503, a genuine
  all-files-gone ghost still cleans up, and anything else raises with rows kept
  (`TrashMoveIncompleteError`). Genuine ghost cleanup is pinned unchanged; duplicates
  resolve and import Replace inherit the guard. Behaviour change kept deliberately:
  deleting a no-album artist with the root unavailable now 503s instead of returning
  `trashed_albums=0`.

- **`GET /api/config` serves the user's raw `config.yaml` — every credential in it,
  unmasked — to any caller who can reach port 3030.** (Found 2026-08-28, auth-posture
  audit.) `yaml_text` is documented verbatim as the raw on-disk text with "secrets are NOT
  masked here" (`app/models/config_api.py:16-20`); the sibling `effective_yaml` is
  redacted through two passes (confuse's `redact` flag plus the `SECRET_KEY_PATTERN`
  safety net), but `yaml_text` bypasses both — deliberately, because Save writes it back
  verbatim and masking once clobbered list-nested credentials. So the third credential
  store — the user's own beets config (`lyrics.genius_api_key`, `spotify.client_secret`,
  `acoustid.apikey`, `subsonic.pass`, beets' own `plex.token`) — is served in full,
  unauthenticated, while MusicDrop's two settings stores got 0600 files and write-only
  APIs. Not a coding error: it follows correctly from "the editor edits the raw file" and
  is only wrong because there is no caller identity. Auth is the fix (Next up #4 and the
  vault note `musicdrop-auth-posture`); there is no sensible in-place patch that preserves
  the round-trip editor. Recorded here because it is the strongest argument that the auth
  item is mis-sized as "long-term".

- **`GET /api/albums/{id}/cover` 500s on an album whose first track is not a parseable
  audio stream.** (Observed 2026-08-29 on the stage stub `.mp3`s; PRE-EXISTING, not
  introduced by `fix/bank-dup-enforce`.) The embedded-art fallback constructs
  `MediaFile(track_path)` unguarded (`app/beets/library.py:751`, reached from
  `get_album_cover` whenever `artpath` yields nothing). mutagen's `HeaderNotFoundError` is
  reraised by `mediafile.utils.mutagen_wrapper.mutagen_call` as
  `mediafile.UnreadableFileError` — verified in the pinned venv, message "can't sync to MPEG
  frame" — and nothing between there and `get_album_cover_endpoint`
  (`app/api/albums.py:231-290`) catches it, so a truncated, half-copied or otherwise corrupt
  file in a real library answers 500 instead of the endpoint's declared 404. Both sizes are
  affected: `size=thumb` reaches the same call through the thumb cache's producer lambda.
  Wanted: treat an unreadable audio file the same as "no art" — the 404 the frontend already
  degrades into its placeholder — rather than an error the album grid cannot render around.
  `cover_validator` is unaffected (it only `stat`s), so a corrupt file still yields an ETag
  and the 500 is only reached on a validator miss. The sibling `MediaFile` reads (track
  detail, import candidate cover) want the same audit; this entry is scoped to the one path
  with a probe behind it.

- ~~**Every `.m3u8` export goes stale on a reorganize or an album tag edit — only the artist
  rename re-exports.**~~ — **FIXED in #195,
  2026-08-29, per the owner's all-movers decision (vault decisions 24).** One sync core
  (`app/playlists/reexport.py`, async wrapper kept for endpoints) wired into album edit,
  album/artist delete, duplicates resolve + resolve-all, reorganize apply, disk-sync apply,
  and the import `replace` duplicate action — a seventh mover the entry below missed; each
  result/status model carries `playlists_reexported` (rename's existing field name).
  Residuals, recorded deliberately: (a) every FAN-OUT mover — a handler that loops N units
  with the re-export running only after the loop returns cleanly — loses the collateral for
  units already moved when a later unit raises: artist delete (share drops mid-loop),
  duplicates resolve with several losers, and resolve-all (which absorbs only
  `StaleGroupError`; any other fault propagates — `app/beets/duplicates.py:474-483`) all
  share the shape (probe-verified 2026-08-29: a resolve-all 500 left group 1 trashed with
  its export still naming the dead track). Single-unit movers are safe — a failed trash
  drops nothing. The 500 carries no body to report through; (b) import `merge` destroys the old item ids,
  so re-export cannot repair those playlists (entries go permanently unavailable) — known
  gap, needs its own design; (c) trash restore is a structural no-op (re-import mints new
  ids) — and beets' rowid REUSE (`import_session.py:549-551`) means a restore can land on
  an id a playlist still references, a pre-existing hazard this work neither created nor
  fixed; (d) the import-replace count is a log line only (the import worker has no result
  channel for it); (e) a disk-sync aborted by `LibraryRootUnavailableError` deliberately
  skips re-export — every track looks missing at that moment, and re-exporting would empty
  every `.m3u8`; (f) three flows re-export but report NOTHING — album/artist delete and the
  single-group duplicate resolve carry `playlists_reexported` in their result models yet
  have no success-outcome surface at all (delete dialogs close and navigate, a resolved
  group just vanishes from the refreshed report; artist rename likewise navigates away on
  clean success), so the count stays silent there — status quo for those flows, its own UI
  work if ever wanted (2026-08-29 review finding). Original entry:
  (Found 2026-08-28.) The exports under `<music>/.playlists` embed
  track paths RELATIVE to the export dir, and `reexport_playlists_containing`'s own
  docstring states the invariant ("a batch of file moves leaves every existing export stale
  until the playlist is next mutated") — but its only caller outside `app/api/playlists.py`
  is the artist rename (`app/api/artists.py:275`). The reorganize runner, which can move the
  entire library, never calls it; `edit_album_endpoint` calls `apply_album_edit_op` and then
  only `emit_library_changed`. And tag edits move files BY DEFAULT under the shipped starter
  config: beets' `should_move()` returns move OR copy, and `config.starter.yaml` ships
  `copy: yes`. So the two most common file-moving operations silently break every Plex
  playlist containing the moved tracks — no warning in the preview, no line in the job
  result, no signal until dead entries show up in Plex. Delete-to-Trash and disk-sync
  removals leave the same stale rows. Needs a short design conversation first (re-export on
  which events, and what the job result reports); the wiring after that is mechanical.

- ~~**A banked duplicate decision is silently DISCARDED when the apply's re-detection
  misses — the album imports anyway and the row reports `done`.**~~ (Found 2026-08-29 by the
  release-id-pin deep review; probe-CONFIRMED against beets' real `_resolve_duplicates`
  with the production dup guard installed.) — **FIXED on `fix/bank-dup-enforce`
  (PR #197), 2026-08-29, per the owner's settled design (vault decisions 25):
  enforce the decision from the banked prompt's stored library album ids instead of trusting
  beets' name-keyed re-detection.** Three arms, because the four actions are not equally
  forceable: `skip_new` short-circuits in the apply runner (`app/bank/apply_runner.py`,
  `_skip_new_is_enforced` between the staleness checks and `directive_for`) — if a stored
  colliding album still survives, NO import is started and the row resolves `done` with no
  album id, which the Review page already renders as "Kept your existing copy" with no
  frontend change for that arm (the diff does change `BankReviewPage.tsx` elsewhere — the
  album-id branch and the queued copy); `replace` rides a new internal
  `BankApplyDirective.replace_existing` into
  the session, which unions the stored ids into the SAME post-run Trash pass the hook-recorded
  ids use (`WebImportSession._seed_replace_from_directive`); `merge` cannot be forced at all
  (beets performs it inside the hook) so it is REPORTED honestly — an album that landed with
  no `needs_dup_resolution` on the feed now fails with wording that says a second copy landed
  and steers to removing one, never to a blind re-decide that would import a third;
  `keep_both` is untouched. Every stored id is identity-verified before it is acted on
  (`duplicate_albums_still_present` in `app/beets/library.py`): beets ids are reused SQLite
  rowids, so presence alone would let a `skip_new` refuse an import over a stranger and a
  `replace` Trash an album nobody decided about. The replace seed is additionally gated on
  the run having actually landed something, and excludes this run's own landed ids (a
  reused rowid would otherwise Trash the album just imported).
  Residuals, recorded deliberately: (a) **`merge` stays report-only** — the row fails with
  guidance, nothing is merged retroactively and nothing is undone; the album really is in
  the library twice until the user removes one. (b) **Up-front-resolver dup decisions keep
  the trust-the-hook path** — a `duplicate` decision posted on a row with no banked prompt
  (`item.duplicate is None`), or a prompt that listed no existing album, has no stored id to
  enforce, so it behaves exactly as before. (c) **The identity check can read a re-tagged
  copy as gone.** It compares album-artist + album (case-folded) and, when both sides carry
  one, the release URL — and when the stored name key is only half filled, a stored URL must
  be matched by a live one or the entry fails shut. An album RENAMED or re-tagged to another
  release since banking fails the match and counts as not surviving — so a `skip_new` would
  then let the import run, and a `replace` would leave that copy in place. That is the
  deliberate direction (never act on an album we cannot confirm), but it means enforcement is
  not total: the drift case it does not cover is a stored copy whose identity moved, and the
  remedy is a rescan. The REPORTING of that case is honest, though: a `replace` whose banked
  copies have all gone or drifted (and whose run beets did not re-detect either) now fails
  with wording that says the album was imported and no old copy was moved to Trash, and steers
  to checking for a leftover — it no longer reports `done`, which the Review page renders as
  "Replaced / the old copy was moved to Trash".

  The original diagnosis, kept because it is why the fix looks like this: beets consults
  `get_duplicate_action` only
  when `task.find_duplicates()` finds a hit, and that query keys on the CHOSEN release's
  albumartist+album (`duplicate_keys.album`, beets `config_default.yaml:51`; guard at
  `stages.py:334-341`, query at `tasks.py:368-399`). When the chosen release's naming
  differs from the library copy's — an unpinned legacy re-run ranking a different edition
  first, or even a PINNED row whose colliding album was renamed or removed between bank
  and apply — the hook never fires and the decision evaporates: `skip_new` ("keep
  existing, import nothing") IMPORTS the album; `replace` never populates
  `_replace_album_ids` (only `_beets_dup_action`, reached from the hook, fills it —
  `import_session.py`'s `_beets_dup_action`), so nothing was trashed and TWO copies remained;
  `merge` imported as a separate album; only `keep_both` landed as intended. `_classify`
  then read `album_id is not None` as success → `done`, so there was no signal anywhere.
  The release-id pin (#196) NARROWED this — a pinned row against an unchanged library
  re-detects (probe-verified) — but did not close the drift case, which is what the stored-id
  enforcement above is for.

- ~~**Bank apply re-runs the match instead of replaying the user's chosen release — every
  sweep-banked DUPLICATE row, by construction.**~~ (Found 2026-08-28.) — **FIXED in #196, 2026-08-29, per the owner's settled design
  (vault decisions 24): store the reviewed release id at bank time and pin the apply to
  it.** The sweep's
  duplicate banking site (`app/beets/import_session.py`, `get_duplicate_action`'s sweep
  branch) now stores the release the task was MATCHED to as the same `ParkedAlbum` payload
  a `needs_review` row carries, so `directive_for`'s existing "decide once" path pins
  `import.search_ids` to it. No apply-side change was needed and the OpenAPI contract did
  not move (`BankItem.parked` was already in it — verified by re-running both regen steps).
  Residuals, recorded deliberately: (a) **legacy rows stay honestly unpinned** — rows
  banked before this change carry no payload, so their apply still re-runs the lookup —
  and a re-run whose top match dodges duplicate detection used to DISCARD the decision
  entirely, importing against a "skip new" (the struck entry directly above, now fixed on
  `fix/bank-dup-enforce`: a legacy row's stored prompt still enforces `skip_new`/`replace`
  by id, so only the release CHOICE stays unpinned on those rows);
  nothing back-fills them and they drain by user decision. The honest labels ship in the
  SAME PR, on both bank decision screens: the duplicate screen's note (which names the
  Rescan remedy — a rescan re-banks the row through the pinning path) and the candidate
  screen's selected-option note. (b) **Id-less sources stay
  unpinned** — an option whose `release_id` is None (a source that carries no release id)
  has nothing to pin, unchanged from before. (c) **The pin is machine-matched, not
  user-chosen**: on a sweep-banked dup row the release was picked by beets' auto-apply, not
  reviewed by a human — the fix makes the apply replay *the decision the sweep actually
  made* rather than a fresh one, which is what "decide once" can mean for a row nobody
  looked at yet. A user who wants a different release still has search/rescan on the row.
  (d) The bank's lazy `/duplicates` re-check stays gated OFF for `needs_dup_resolution`
  rows (`app/api/bank.py`): such a row's collision is the banked prompt its screen renders,
  and its new parked payload exists only to pin the apply.

- ~~**A Trash row holding no importable audio can never be restored through the UI**~~ —
  **RESOLVED in #189, with a premise correction found by the deep review.** `track_count 0`
  means "nothing here produced a readable media Item", NOT "no audio": beets' importer
  restores formats the listing cannot read (`.tak`/`.ra`/`.dff`, truncated files), so the
  first fix (disable Restore) was itself a new data-loss path and was reverted the same
  day. Shipped shape: the zero-track row keeps Restore enabled and explains the
  uncertainty ("MusicDrop couldn't read audio tags here — Restore may still work"), wired
  to the button via `aria-describedby`; a genuinely media-free folder still reports
  `could_not_restore` and Empty remains its only exit. Follow-up, recorded as its own
  entry below: record the origin at trash time so a true move-back restore becomes
  possible.

- ~~**The orphan sweep moves a multi-disc album's art/booklet folder to Trash whenever
  its name is outside the hardcoded `ART_DIR_NAMES` list**~~ — **FIXED in #189.** Sweep
  and preview both drop candidates a live album owns, derived from the beets path template
  (the album's destination dir — or one disc level above — when it strictly contains the
  album's actual root), so `Scans (LP)` inside a multi-disc album is protected whatever
  its basename while genuine artist-dir husks still sweep; survives an album nested inside
  another album's directory. `ART_DIR_NAMES` stays as the backstop for non-album-owned
  art. Bounded claim, stated in the code: protection covers an album FILED WHERE THE
  TEMPLATE PUTS IT. Residuals recorded 2026-08-28: art left in a VACATED dir after an
  album move is still sweepable (that dir is no longer any album's); a split album (items
  across two folders) contributes no protection and falls back to `has_own_audio`;
  query-keyed path formats disagreeing about disc nesting are read through the first
  album's shape (less protection, never more).

- **Record the origin path at trash time so a move-back restore becomes possible.**
  (Follow-up from #189.) The trash layer is a bare `shutil.move` with no metadata anywhere
  — origin is verifiably unrecoverable, which is why a genuinely media-free Trash row's
  only exit is permanent Empty. One sidecar record (or a manifest) at trash time makes a
  true restore possible for every future row; old rows stay import-restore-only. Small,
  wants a short design look at where the record lives (per-folder file vs one manifest)
  and what Empty does with it.

- **The delete-path mount predicate accepts a root with ANY entry, so a stray file on a
  local mountpoint masks a dropped share.** (Deferred 2026-08-28 by design call, from the
  #189 security review.) `.stfolder`, `lost+found` or an empty leftover dir on the
  mountpoint makes `require_library_root` pass while the share is gone, re-opening the
  ghost drop for exactly that state — though #189's post-condition in `trash_album` now
  catches the damage for the moved-nothing case. Proposal on file: a
  `require_library_present()` variant sampling K live album dirs from the DB — strictly
  stronger for delete; must NOT replace the shared default (disk-sync calls the predicate
  per removal). Disk-sync accepted the identical residual for itself.

- **`useDiskSync.ts:45` throws a hardcoded "Library folder unavailable…" for ANY 503,
  discarding the server's message.** (Found during #189.) The backend now differentiates
  empty vs unreadable (with strerror); the disk-sync UI shows the wrong sentence for both.
  Delete dialogs render the real detail — this is the disk-sync path only. Trivial:
  surface the server's `detail` when present.

- **"Save art to library" is a one-click, unconfirmed action that permanently deletes
  hand-placed `artist-poster.*`/`artist-background.*` — and fires as rename collateral.**
  (Found 2026-08-28.) `POST /api/artists/art/apply` runs `force=True`
  (`app/api/artists.py:931`), and `_write_one` under force unlinks EVERY existing
  `artist-<kind>.*` before writing MusicDrop's resolved image
  (`app/beets/artist_art.py:82-84`) — the exact Plex Local Media Assets filenames a Plex
  user curates by hand. Deleted, not trashed, behind a bare `IconAction` that reads as
  additive; every other destructive action in the app sits behind an AlertDialog. The same
  `force=True` fires automatically on a rename when items moved and the write toggle is on
  (`artists.py:266-272`) — so merging artist A onto B silently replaces B's curated poster
  with whatever Deezer resolved, and the rename dialog never mentions art.

- **`write_artist_art` reports `status="written"` when only some folders wrote** — the same
  swallowed-substep shape as the album-art divert. (Found 2026-08-28.) The per-directory
  loop catches `OSError` into a `failed` flag that the status ladder consults only when
  `written == 0` (`app/beets/artist_art.py:123-129`); `ArtistArtOutcome` carries no error
  field, so a partial failure reaches neither the wire nor the job tally, and the model's
  own docstring defines `failed` as "every write attempt errored" — the partial case has no
  representable value. Trivial once the reporting shape is chosen. Related smaller lie, same
  family: `ArtistRenameResult.playlists_reexported` counts playlists whose `.m3u8` write
  FAILED (`_export_playlist` swallows every exception; the counter's docstring says "a
  failed write still counts") — the honest shape is written vs attempted.

- **`download_image` validates only the FIRST and LAST redirect hop, and issues the
  intermediate requests anyway.** Moved here 2026-08-28 from Deferred minors, where a blind
  SSRF sat under a heading that says "cosmetic / self-healing". Measured:
  `public → 127.0.0.1:9 → public` returns bytes and the internal GET happens
  (`app/artwork/download.py:120` streams with `follow_redirects=True`, and only the initial
  and final URLs pass `assert_public_url`). The comment directly above asserts the exact
  protection the code does not provide, so a reviewer reading only the comment concludes it
  is hardened. Three live callers (fanart, spotify, deezer). The fix is already written ~60
  lines below in the same file: `fetch_image_bytes`'s manual hop loop (`:180-193`,
  `follow_redirects=False`, `assert_public_url` per hop). Severity bounded by the CDN set,
  but this is the loose path — and it handles the semi-trusted input.

- **The mypy `disallow_untyped_calls = false` exemption list has rotted: 96 of its 102
  modules no longer need it.** (Measured 2026-08-28 with a probe config in the session
  scratchpad — the repo was not touched.) The override's rationale ("beets is untyped; the
  adapter absorbs that", `backend/pyproject.toml:57-59`) is false outside `app/beets/`: the
  list blankets four HTTP API modules (`app.api.lyrics`, `.reorganize`, `.stats`,
  `.disk_sync`), all four `app.plex.*` modules (which import no beets at all), and five job
  runners — so the next untyped call added to any of those is silently accepted and CI
  stays green, against hard rule 1. Removing all 102 entries leaves exactly 17 errors in 6
  files (`app/plex/client.py`, `app/beets/{rename,lyrics,edit,cover}.py`,
  `tests/test_plex_sync.py`). Small: shrink the list to the modules that still need it.

- **Hard rule 3 (no `import beets` outside `app/beets/`) is claimed "enforced in CI" and
  nothing enforces it.** (Verified clean 2026-08-28 — the boundary holds today, which is
  exactly when a guard is cheapest to add and most likely to be assumed present.) Rules
  1/2/4/5 each have a real gate (mypy, the spec guard, ruff, pytest); rule 3 has no ruff
  rule (no `TID`/flake8-tidy-imports in the select list at `backend/pyproject.toml:46`) and
  no test walking the tree. One banned-import lint rule or a five-line test closes it.

- **Three image-serving GETs declare `application/json` while returning image bytes — the
  defect their sibling route's own comment exists to prevent.** (Found 2026-08-28.) Six
  endpoints return image bytes; three declare `200: {"content": {"image/*": {}}}` and three
  declare nothing, so the contract falls back to JSON: the album cover
  (`app/api/albums.py:220`), the import candidate cover (`app/api/import_.py:195`), and the
  playlist artwork GET (`app/api/playlists.py:653`). The rule is spelled out at
  `albums.py:284` ("without this entry the generated client is offered a JSON body and
  never told about the binary one") and honoured at `artists.py:365`. Nothing breaks today
  — all three are consumed via `<img src>` — but the generated types tell any future typed
  caller the body is JSON, and `openapi-fetch` would call `res.json()` on a JPEG. Trivial
  per route, and it IS a contract change: the two-step regen applies.

- ~~**README drift, five items (2026-08-28 sweep — each violates the keep-README-in-sync
  rule).**~~ — **FIXED in #194, 2026-08-29.** All five confirmed by a re-derive-and-refute audit (17 findings
  total, 0 refuted) and fixed, along with 12 more the full sweep found: the missing
  `MUSICDROP_PLEX_LIBRARY_SECTION` seed var (empty = the SOLE music section; several →
  sync refuses until one is named — the "first artist section" comments in the code are
  the drifted ones), `MUSICDROP_LYRICS_BACKFILL_DELAY_SECONDS`, the artist-image toggle
  env names at the backup list, the #189 husk-sweep protection and marker-both-directions
  wording, the #192 art-collision refusal (Edit tags + Reorganize bullets), docs-only
  merges cutting no release, and the path-fix range being v0.34.1 (not v0.34.0). Two
  details of the recorded entry were themselves wrong (right-THAT, wrong-HOW again):
  the fanart key name WAS in a tracked file (`docker-compose.yml:15`), and "silently
  Deezer-only" overstated — README:42 already documented the chain and "Deezer needs no
  key"; the missing part was specifically HOW to enable fanart.tv/Spotify.

- **The candidate-review screen says release art is "not applied" — the config editor can
  falsify that, and the comment's own Revisit trigger has fired.** (Found 2026-08-28.)
  `CandidateReview.tsx:236-238` reasons from "the default config (no fetchart/embedart)"
  and `:315` says "Revisit when the config/art slice can enable fetchart" — that slice
  shipped: `PluginName` admits `fetchart` and `embedart`, `setup_beets` loads the user's
  plugin list at call time, and `run_import_worker` forces only import flags, never
  plugins. For a fetchart user the caption "Release art · not applied" is false and
  `WhatChanges` omits art — the same class as the #184 tooltip lie, on the screen whose one
  job is saying what the import will change. Not executed against a fetchart-enabled
  import; verify that first, then make the caption read the live plugin list.

- **`MUSICDROP_TRASH_DIR` is an unvalidated `rmtree` root.** (Found 2026-08-28.)
  `resolve_trash_dir` returns `Path(settings.trash_dir).resolve()` with no containment
  check (`app/beets/trash.py:245-247`), and `empty_all` then `shutil.rmtree`s every child
  of whatever came back (`trash_manage.py:170-174`). Nothing asserts the trash dir is not
  the music root, not inside it, and not the beets dir — and pointing trash at the music
  dataset (so deletes are same-filesystem renames instead of cross-device copies; the
  default sits on the small `/data` volume per README:127) is a plausible operator move one
  typo away from `MUSICDROP_TRASH_DIR=/music`. Every other destructive path here has a
  containment check (`resolve_trash_child`, `_folder_is_shared`,
  `orphans._excluded_predicate`); the trash root has none. Small: refuse at startup when
  trash resolves inside or equal to the music dir or the beets dir.

- **Vacuous-pin audit: sized 2026-08-28; the four confirmed pins FIXED in #190** (two dead
  absence needles in the reorganize adapter replaced with positive pins on the exact
  emitted strings — the divert message is pinned verbatim — and three frontend absence
  pins on never-existing copy deleted with the reason). #190 also converted the three
  `vi.mock` + `await import()` no-op files (the third lives at
  `pages/settings/LyricsBackfillPanel.test.tsx` — the `components/lyrics/` path earlier
  records carried was stale). Remaining audit population below, unchanged.** Population:
  5,975 backend `assert` statements (ast-counted, not grepped) and 2,522 frontend
  `expect(` calls; the absence-shaped frontend subset is 291, of which 133 are
  copy-bearing. A needle-absent-from-source filter cut those 133 to 11 candidates;
  adjudicated: **4 confirmed vacuous** — `ArtistAlbumsPage.artwrite.test.tsx:140` (the
  `/1 written/` copy is structurally unreachable in the rendered tree, so the pin passes
  with the whole artist-art tally deleted), `ReleaseSearchRow.test.tsx:68` (asserts the
  absence of paragraph copy that never existed), `ImportPlaylistsPage.test.tsx:843`
  (`/no library match/` appears nowhere in production), and
  `test_reorganize_adapter.py:597` (a 48-character sentence `_verify_moves` has never been
  able to emit) — plus one suspected (`test_reorganize_adapter.py:270`). The previously
  recorded "~42 exact-string pins" figure is unsourced — no counting definition reproduces
  it. #185's sweep covered ONE shape (the 192 `not.toBeInTheDocument()` pins), frontend
  only: the 94 `toBeNull()` absence pins, the 33 `not.toHaveBeenCalled()`, and all 82
  backend `not in` pins were never audited. Known false-positive modes for whoever runs
  the remainder: ARIA roles are correctly absent from source, `/i` regexes need
  case-insensitive matching, and JSX line-wrapped copy needs whitespace normalization.

- ~~**Album art can still be silently diverted — and on the tag-edit path it churns.**~~ —
  **FIXED on `fix/album-art-divert-preflight` (PR #192, squash `3cf1882` = v0.45.1), 2026-08-28.**
  `art_preflight` (`app/beets/reorganize.py`) is the third whole-app move-hygiene helper
  beside `collisions_by_dest` and `carry_sidecars`: it predicts the art destination
  exactly as `Album.move` hands it to `move_art` — the first SURVIVING, source-present
  mover's destination dir — and refuses BEFORE anything moves. All three entry points are
  covered: reorganize refuses the unit (preview mirrors apply; new `"art"`
  `ReorganizeCollisionKind`, contract regenerated two-step), the tag edit refuses its
  whole move phase (tags still write; a track with its OWN collision keeps its own
  detail), and artist rename inherits via `apply_album_edit` with failure isolation
  pinned. The edit path's `move_art` now receives `item_dir` of the first item that
  actually MOVED (mirroring `Album.move` — the bare call followed the FIRST item even
  when it never moved). Post-move backstops on both paths report a divert that races
  past the pre-flight; reorganize's compares BASENAMES only, because a `unique_path`
  divert always changes the basename and never the dir — a dir-only difference is a
  misprediction, not a taken name. The trap this entry warned about was sidestepped: art
  never enters `collisions_by_dest`; the predicate owns its exemptions (byte-equal own
  art; occupants the unit itself vacates, samefile included). A samefile ALIAS of the
  album's own art is deliberately NOT exempt — beets' `move_art` has no samefile guard
  and diverts it (pinned with a symlink test). The heal is pinned too: a previously
  diverted `cover.1.jpg` renames back to `cover.jpg` on the next clean move, because
  `art_destination` discards the `.1` base. Verification: red-first repros on both
  paths, 10/10 deep-review mutations killed plus 5 more found-and-pinned in the fix
  rounds, browser-verified conflict rendering. Residuals (2026-08-28): (a) a mover whose
  move RAISES mid-batch can still shift the actual art dir off the prediction — the
  backstops report it (pinned via silenced-pre-flight race tests on both paths); (b) a
  template that cannot render degrades to no-prediction at WARNING, disarming refusal
  AND backstop for that album — deliberate never-fail-the-sweep posture; (c) `trash.py`'s
  third `Album.move` site stays un-preflighted (not live: `_unique_trash_dest`
  guarantees an empty container); (d) healing EXISTING diverted names is not built — one
  heals on its next move-bearing operation or cover install; (e) the preview reads
  pre-move disk state, so two albums converging on one folder surface the art collision
  sequentially at apply time, the same bound item collisions already have.

- ~~**`static_dir` gate mismatch (residual of the logged-posture fix).**~~ — **FIXED in
  #191** (2026-08-28) exactly as this entry's 2026-08-28 correction prescribed: one startup
  WARNING where the condition is already computed (`static_files.py`'s bare return), naming
  the configured path, the consequence (production posture active, no SPA served) and the
  realistic cause (a stale `MUSICDROP_STATIC_DIR` in `backend/.env`). Fail-closed behavior
  unchanged, no new knob, both gates untouched, `container_music_default` untouched; the
  withdrawn index.html-keyed fix stays withdrawn (FAIL-OPEN). One of this fix's tests was
  deleted by the cross-slice review as unfalsifiable (it asserted on a capture window it
  opened after its own guard); dev posture stays pinned by the real-app catchall test.

- ~~**Bank store sink has the same inf/NaN shape the playlists store just fixed**~~ —
  **FIXED in #191** (2026-08-28) per this entry's own corrections, with the withdrawn
  "straight port" staying withdrawn: `_confidence` clamps at the single producer
  (non-finite → 0.0; all five sinks verified to flow through it), `_parse_row` heals
  poisoned rows already on disk (to 0.0, never None — the REQUIRED nested candidate floats
  must not validation-fail into silent invisibility), and `_row_text` gained
  `allow_nan=False` as the loud guard against any future leak. Wire tests run over
  SurrogateSafeJSONResponse (stock FastAPI nulls the value and false-passes — as this entry
  warned). Residual: a healed row's cached index summary keeps the stale value until
  rebuild — out-of-band by design.

- ~~**`get_playlist` propagates `UnicodeDecodeError` on a non-UTF-8 record file**~~ —
  **FIXED in #191** (2026-08-28) as the full read-posture class this entry's re-verifications
  mapped: one read posture per store (missing / unreadable / undecodable / malformed →
  absent, both single-record readers matching their list twins); playlists DELETE-on-corrupt
  returns 204 and removes the file, making the route match its own comment; a corrupt bank
  row is purgeable unless the queue's own index says `applying` (owner posture 2026-08-28 —
  an unreadable row cannot testify about itself); bulk deletes skip-and-report; the apply
  drain moves past corrupt queued rows; list-side skips log WARNINGs naming file and reason.
  First raw-byte non-UTF-8 tests either store has ever had. Deep review attacked the purge
  race with a per-row three-way barrier (0 stolen rows across genuinely colliding trials)
  and a 10-shape hostile-id battery — held. Residuals: `reset_bank_index` rebuilds forget a
  corrupt row's `applying` status (documented at the definition; test-only helper today —
  wiring it to production needs the runner's in-flight id preserved across the rebuild);
  a corrupt row still appears in listings from the cached index until a rebuild, its detail
  reading absent — visible-then-absent beats invisible-and-permanent.

- ~~**Config editor accepts `import.autotag` that MusicDrop-driven imports cannot honour**~~ —
  **SHIPPED in #191** (2026-08-28) as the full advisory channel (the owner's pick over the
  cheap patch, decided 2026-08-28): `ValidateResponse.advisories` + deliberate rules for
  `autotag`, `duplicate_action`, `singletons`, `incremental` (the last honestly described as
  honoured-but-trapped), rendered as neutral StatusBanners (`role="status"`) in the editor.
  The restore leak (`duplicate_action`/`threaded`) is closed INCLUDING the
  `InLibraryCopyError` early exit the deep review caught — mutations now start below the last
  raise, with a red-first pin on the refusal path; the `singletons` force is hoisted to every
  import path (a `singletons: yes` user config can no longer make a review import silently
  import nothing); starter.yaml's autotag comment tells the truth. Residuals (2026-08-28):
  advisories carry no line/column, so the UI is a list, not a CodeMirror gutter hint at the
  point of edit (`_line_col_for_path` in `app/beets/config_editor.py` exists if that follow-up
  is wanted); `threaded` (forced every run, lives outside `import:`) and `move`/`copy`
  (overridden whenever the UI passes a move choice) sit outside the rule set by scope — the
  boundary is recorded only in the rules tuple's docstring; the LIVE `data/beets/config.yaml`
  still carries the old misleading autotag comment (runtime file, not repo — the editor
  advisory now warns at edit time); deploy-skew: an old backend with a new bundle crashes the
  advisory render (strict required field, consistent with the codebase's contract posture).

- **Untested defensive lines** (deep-review survivors, all currently benign — pin when
  touched next): broken-symlink sidecar carry (`sidecars.py` `lexists`), singleton
  crash-path sidecar carry, `edit.py` `_inside_library` guard (pre-existing from main),
  dismiss double-click swallow,
  from the art pre-flight (2026-08-28): `art_preflight`'s `not old_art` / `not moving`
  early returns and its render-failure `except Exception` degrade arm, and the edit
  backstop's recompute-against-`moved_dir` choice (comment-justified; no test
  distinguishes it from recomputing against the pre-flight's dir),
  the aria-disabled-not-disabled focus rule (the "Pagination rule" is convention, not test).
  (The disk-sync first-dir-wins clause left this list in #191 — replaced by commonpath, `"."` pinned.)
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

- ~~**Disk-sync emptied-row path shows the first item's folder,** not the album root~~ —
  **FIXED in #191** (2026-08-28): `_scan_item` folds a running commonpath per album id
  (`_common_rel_dir`), the first-dir-wins tradeoff comment went with the code it described,
  and the library-root `"."` case is pinned. Residual: items outside the music dir under
  `import.copy: no` still render an escaping relpath (`../inbox/Album`) — display-only,
  recorded, not worth a mapping layer today.

- ~~**Artist-image cache: after a broken cache dir is repaired, affected artists never return
  to disk.**~~ — **FIXED on `fix/artist-image-cache-tier` (PR #198), 2026-08-29,
  per the owner's settled design (vault decisions 24): the in-memory stand-in becomes a real
  cache TIER with LAZY write-back on the next touch, no background job.** Two halves, both in
  `app/artwork/cache.py`. (1) `validator()` now consults the tier after disk: a strand carries
  its own quoted-strong tag (`"mem-<sha256[:16]>-<size>"`, minted once in `_MemoryFallback.put`
  and stored on a `_MemoryEntry`, never recomputed on the read path), so a stranded artist takes
  the ordinary 304 / `_serve_cached` route and never reaches `filler.fill` — which kills the
  ~2 Hz remount loop during the outage as well as after repair, and with it the per-request
  sha256 in `_serve_full` and the per-request thumb re-derive. The tag families are disjoint by
  construction (a stat tag opens with a digit), which is what `get_thumb` reads to refuse the
  recorded hazard: a `.thumb` pair keyed to a memory tag is derived and SERVED but never
  written, because no restart could reproduce the tag and no sweep looks for it. (2) `get()`
  re-attempts the disk write on a memory hit (`_write_back`, through the same
  `_publish_positive` helper `store_positive` uses, so the `.miss` unlink and the
  mime-before-bytes order cannot drift); success DROPS the strand exactly as `_or_remember`'s
  success branch does, failure is swallowed and leaves the entry untouched — deliberately not
  re-put, since re-putting would refresh its eviction position and turn a bounded oldest-first
  map into an access-ordered one. Negatives are excluded from the write-back: they self-heal on
  TTL and a `.miss` conjured from a probe would bar the re-resolve that repairs the key.
  `api/artists.py` needed no change. Twelve mutants run, every one killed;
  `test_clear_auto_forgets_an_in_memory_fallback_entry` was reworked because its "unwritable"
  dir was an ordinary missing path, so the write-back would have emptied the map in its own
  setup and left it green for the wrong reason — the new fixtures use a dir whose parent is a
  regular FILE (ENOTDIR on `mkdir`, root-safe unlike `chmod`). **Residuals, all by design or
  pre-existing:** a restart before the next touch still loses the memory copies (accepted — they
  re-fetch); the rename merge / `kept_target` paths still discard the old key's strand without
  carrying it (pre-existing — only `_rename_move_all` migrates one; the discard itself is now
  pinned by `test_rename_purging_the_old_key_forgets_its_strand` and
  `test_rename_moving_a_pin_onto_an_auto_image_forgets_the_sources_strand`, since the merge tests
  are disk-only and never reach the memory half);
  and the other recovery paths named below (the backfill sweep's own cache instance, a manual
  override, `clear_auto`) remain as they were. **Original entry, kept for the diagnosis that
  shaped the fix:**
  Read "to disk" literally — the portraits keep SERVING, correct bytes and correct
  content-type, on every request, out of the in-memory stand-in. What never recovers is
  persistence: the on-disk slot stays absent for the process lifetime. Nothing user-visible
  breaks. The stand-in (`ArtistImageCache._MemoryFallback`, added in the perf/images wave) is
  dropped by `_or_remember`'s success branch, which runs only when a write is ATTEMPTED and
  succeeds — and `store_positive`/`store_negative` are reached only after `cache.get()`
  misses, so a memory HIT satisfies the request without ever calling either. For those keys
  the write never happens and the discard never fires. Measured: 5 decode+resizes for 5
  post-repair requests vs 1 for a healthy control, with the artist's slot still absent from
  disk (`validator()` stats disk only, so `get_thumb` bails and every request re-derives).
  **Which state actually survives (2026-08-27 re-verification):** a stranded SUCCESSFUL
  `CachedImage` whose write failed — not a negative marker. The negative half self-heals:
  `_NegativeUntil` carries an absolute expiry, `get()` discards it once stale and
  `has_fresh_negative` reports False, so the key re-resolves and the now-succeeding write
  both repopulates disk and drops the entry. Read the `store_positive`/`store_negative`
  phrasing above as describing the positive slot only. Bounded (the map is capped) and off
  the event loop, so this is a silent CPU/latency regression, not a correctness one — but it
  persists for the process lifetime and there is no log after repair, because the writes
  stopped failing; it also costs a restart's worth of upstream re-fetches, since the disk
  cache stays empty. The paths that DO clear it: the Reset endpoint (`clear_auto`), an artist
  rename, or eviction by the map's own bound. Note that `cache.py`'s
  `_or_remember` docstring ("hands authority back to disk without a restart") is true only for
  keys that get written again, which these never are. **Needs a design call before anyone
  codes it:** re-write to disk on a memory hit (simple, but puts a write on the read path), or
  periodically re-probe the dir and flush (more moving parts, keeps reads read-only). Do not
  assume either.
  **2026-08-28 re-verification (pair-reviewed): the cost is a feedback loop, it runs
  during the outage too, and the loop half of the fix is small.** Because `validator()`
  stats disk only, a stranded artist's request skips the 304 path, enters `filler.fill`,
  and every completed fill arms an UNSCOPED `art:changed` — which remounts every VISIBLE
  portrait (`<img key={assetVersion}>`; `loading="lazy"` bounds it to the viewport),
  re-requests them, re-invalidates all 11 query families, and feeds the next cycle: a
  self-sustaining ~2 Hz loop for as long as one tab shows one stranded artist — during
  the outage as well as after repair (repair only stops the log line, which is what makes
  it look post-repair). Monogram artists amplify it: each bump un-fails every 404'd
  portrait. The full-size path (the default) additionally pays a sha256 over up to 10 MB
  per request — `_serve_full`'s own docstring documents that cost. Clearing-path
  corrections: rename MIGRATES the strand (pinned by
  `test_rename_carries_the_memory_fallback_entry` — must keep passing; though rename's
  auto-backfill usually repopulates disk when the write toggle is on), and map eviction
  is unreachable post-repair (nothing is ever inserted again). Two recovery paths the
  entry missed: the artist-art backfill sweep builds its own cache instance and
  `store_positive`s to the repaired dir, and a manual override puts `.override` on disk —
  both end the symptom. The design call narrows: the LOOP half is closed by making
  `validator()` consult the memory tier — `_has_portrait` (`cache.py:561-571`) already
  does exactly the disk-then-memory probe, so the asymmetry reads as oversight, not
  design — while the PERSISTENCE half (re-write on memory hit vs periodic re-probe vs a
  first-class memory tier with its own revalidation tag) is the owner decision.
  Recommended shape: memory-as-tier (fixes the outage window too), then persistence; drop
  the "flush on the next successful write of any key" variant — post-repair, no other key
  ever writes, so it would rarely fire. Hazard any fix must cover: a thumb derived under
  a memory tag writes a `.thumb` pair that outlives the process and is never swept. Two
  pins must keep passing (`test_artwork_cache.py:1013` and disk-takes-authority `~:591`),
  and don't build the regression strand with `chmod` — those tests self-skip as root;
  insert into `_memory` directly.

## Accepted residuals and deliberate decisions (not work)

Nothing in this section is a task. Each item was decided, with its reasoning, and is kept
because a recorded decision is what stops the question being reopened from scratch. Do not
scan here for something to pick up — scan *Open bugs / hardening*. Revisit an item only if
the condition it names has changed.

- **The session cookie omits `Secure` on the plain-HTTP path only (2026-08-30, auth slice
  1; narrowed in slice 2).** Slice 1 set the flag to a static `False`, which this entry
  originally recorded as the accepted end state. Slice 2 made it follow the connection
  (`app/auth/cookies.py::request_is_https`): over TLS — direct, or behind a proxy that
  sets `X-Forwarded-Proto: https` — the cookie IS `Secure`, so the accepted residual is
  now confined to a deployment genuinely served over plain HTTP. It cannot be closed
  there: MusicDrop is browsed by LAN IP over HTTP as a design point, and an unconditional
  `Secure` makes that deployment impossible to sign into at all. This is what the
  ecosystem does — Sonarr/Radarr (`SameAsRequest`), qBittorrent, Gitea, Nextcloud and
  Portainer are all conditional; Authelia is the only unconditional one, and it pairs that
  with refusing plain HTTP outright — *"we won't support websites served over HTTP in order
  to avoid any risk"* (<https://www.authelia.com/overview/security/measures/>, re-verified
  2026-08-30). That pairing is the point: an unconditional `Secure` is only coherent
  alongside a refusal to serve HTTP at all, which is not a trade MusicDrop can make. The cookie is HttpOnly, SameSite=Lax, host-only, Path=/ throughout, so
  on the HTTP path the residual is exactly "as private as the LAN". Recorded at
  `request_is_https` and in README's Authentication section.

- **Logout is client-side only; session tokens are stateless (2026-08-30, auth slice 1).**
  A token stays cryptographically valid until its embedded expiry (≤30 days) — there is
  no server-side session table, so nothing can revoke ONE session early. Deliberate: no
  state to store, reconcile or sweep. Rotating `MUSICDROP_PASSWORD_HASH` invalidates
  every session at once (the signing key is bound to the hash — 2026-08-30 audit fix M3),
  as does deleting `<beets_dir>/session-secret`. Pinned as intended behaviour by
  `test_a_token_copied_before_logout_still_works`.

- **The session-secret create race is narrowed, not closed (2026-08-30, auth slice 1).**
  Two processes hitting first-run together could each mint a signing key; the loser
  re-reads after `write_atomic_bytes` and adopts the winner's, but both could re-read
  before the other's `os.replace`. Accepted because the image runs uvicorn single-worker
  by design — there is no second process in the shipped deployment. Documented at
  `app/auth/session.py::load_or_create_session_secret`.

- **Both integration `base_url` fields are credential-exfiltrating SSRF for whoever holds
  the password; the slskd one is additionally REFLECTED — accepted, and measured
  2026-08-30 so the acceptance is informed.** Both are deliberately unrestricted (see the
  ruling below and the comments at `PlexConfig.base_url` / `SlskdConfig.base_url`). A first
  draft of this entry called the slskd field "strictly worse than its Plex twin"; the
  security seat refuted that and the measurement backs it. **Plex:** one
  `POST /api/plex/test` hands `X-Plex-Token` — full Plex account control — as a *header* to
  whatever host the field names, together with `X-Plex-Platform-Version` (the running
  kernel version — measured on the dev box, and a container shares the host's kernel) and
  `X-Plex-Device-Name` (the hostname), fingerprinting the box for free.
  No log-reading and no second request. The response is not usefully reflected, so it is
  effectively blind. **slskd** is worse in exactly one way that matters — it reads the
  response body back out — plus it grants full path control. That difference had never been
  written down or tested. Measured against a local echo server:
  - `app/slskd/client.py` builds `f"{base_url}/api/v0/application/version"`. A trailing
    `#` on the stored value pushes that suffix into the **fragment**, which is never sent
    — so the wire path is entirely chosen by the setting. Observed wire path for
    `http://127.0.0.1:PORT/latest/meta-data/iam/#` was exactly `/latest/meta-data/iam/`.
  - The `X-API-Key` header — the slskd credential — is sent to whatever host it names.
  - The response body is returned to the caller **untruncated**, as
    `SlskdConnection.version`, so `POST /api/slskd/test` reads it back out. Blind SSRF
    this is not.
  - The one real mitigation is that `httpx.get` defaults to `follow_redirects=False`
    (verified on httpx 0.28.1), so a redirect cannot re-aim the request afterwards.

  **The credential exfiltration is accepted**, on the same reasoning as the import ruling:
  the runtime writer is `PUT /api/slskd/settings`, behind the session gate, and the same
  cookie already grants `POST /api/config/save` — arbitrary beets configuration on the same
  box. It crosses no boundary between MusicDrop **principals**, because there is exactly one
  account. Note carefully what that does *not* say, because the first draft of this entry
  over-claimed it as "strictly smaller than what the cookie already buys" and the security
  seat refuted that: this capability **reaches off-box** (Plex account control, whatever the
  slskd key reaches — lateral movement that survives wiping MusicDrop) and it **defeats a
  confidentiality property the app deliberately implements**, since the settings API returns
  only `has_token`/`has_webhook_secret` and these fields are write-only by design. The SSRF
  turns write-only into readable-by-an-attacker-chosen-host; `config/save` does not.
  Accepted because the writer is gated — **re-open the moment a second, less-privileged
  account exists**, and treat this as one of the first things that must stop being a plain
  settings field.

  **The PATH control is not accepted — it is filed as a Low task** (this reversed a first
  draft that dismissed it as "worth little"; the security seat argued it back and was
  right). Rejecting a `base_url` that carries a query or fragment is three lines in a
  validator, and it is what separates *"connect to a host and see whether it speaks slskd"*
  from *"read `/latest/meta-data/iam/security-credentials/` and get the body back"*. With
  the path pinned, `raise_for_status()` does most of the rest: against a non-slskd host,
  `<host>/api/v0/application/version` is a 404, which raises before anything is reflected.
  It doubles as an operator-footgun fix — today a pasted URL with a stray `#` silently
  retargets the request. Do **not** describe this as fixing the SSRF; it does not touch the
  host or the credential.

      parts = urlsplit(value)
      if parts.query or parts.fragment:
          raise ValueError("base_url must not contain a query string or fragment")

  For completeness, since a reader will ask: `assert_public_url` *would* block the
  link-local metadata target specifically — so the anti-hardening argument is "wrong tool",
  not "no benefit". It stays wrong-tool because it also rejects every correct
  configuration, which disqualifies it regardless.

  **The structural fix worth filing above either** — re-confirm the password before writing
  a secret-bearing settings field (`plex_base_url`, `slskd_base_url`, `webhook_secret`,
  both tokens). That is the only measure addressing the exfiltration rather than the path:
  it turns a stolen cookie into a non-event for all of them. It also closes the asymmetry
  the webhook-secret entry notices under *Open bugs* — the password is scrypt-hashed and
  rate-paced while everything it protects is not.

- **`/import` takes an arbitrary server-side path, and that is deliberate (ruled
  2026-07-07, re-based on authentication 2026-08-30).** Relocated here from
  `docs/superpowers/plans/2026-07-07-important-audit-9.md:19`, which is **gitignored** —
  the ruling that answers "why is there no import allowlist?" lived where a fresh clone
  could not read it, so the question was re-openable from scratch by anyone but the two
  people in that conversation. The owner's words, verbatim:

  > **#5 Import containment:** **DROPPED 2026-07-07** (user challenge, agreed). For this
  > deployment — single-user, no auth, CORS-blocked from browsers — `/import` grants no
  > capability an attacker on the LAN doesn't already have via the other unauthenticated
  > mutating endpoints (`DELETE /albums`, `/reorganize`, `/trash`, tag-edit). Marginal
  > delta ≈ 0; an allowlist is pure workflow friction for ad-hoc `/downloads` imports.
  > Fails "complexity must earn its place." #4 (SSRF) is kept because "make the server
  > fetch an internal URL" IS a distinct capability.

  **The conclusion survives auth; its stated premise does not, and the swap makes it
  stronger.** The argument was never "`/import` is safe" — it was a *marginal-delta*
  argument: an allowlist buys nothing while every sibling mutation is equally open. Auth
  did not weaken that, it repaired it. The comparison set (`DELETE /albums`,
  `/reorganize`, `/trash`, tag-edit) is now behind the session gate, so `/import` is
  compared against gated peers rather than ungated ones, and the delta is still ≈ 0 —
  but now because the whole set is closed, not because it is uniformly open. The clause
  to stop repeating is "no auth"; **CORS-blocked from browsers** was and remains true
  (`app/origin_guard.py`), and it was never the load-bearing half.

  What this ruling does **not** cover, and never did: an authenticated owner's own
  mistakes. `{"path": "/"}` is a well-formed request from a signed-in operator, and the
  footguns are filed as their own entry under *Open bugs / hardening* rather than hidden
  under this
  decision — a threat-model ruling about *attackers* is not a ruling about *typos*.

  **The #4-vs-#5 tension is real and is resolved here, not left implicit.** The same
  document kept the artwork SSRF guard (#4) on the ground that *"make the server fetch an
  internal URL" IS a distinct capability* — yet `plex_base_url` and `slskd_base_url` make
  the server fetch exactly that, with no `assert_public_url` in front of them, justified
  by a different argument entirely ("admin-controlled"). Both calls are correct; the
  distinction was simply never written down, so the two comments read as a contradiction.
  It is this: `assert_public_url` exists to stop a *value that should point at the public
  internet* from being aimed inward. `base_url` is the opposite — pointing inward is its
  entire job (`http://192.168.1.50:32400`, `http://plex:32400`, `http://slskd:5030` — the
  last two are this repo's own test values — plus `http://localhost:32400`), so the same
  guard applied there would reject every correct configuration. Measured 2026-08-30:
  `assert_public_url` refuses all four. The enforcement that makes it safe is now
  authentication, not a URL shape. **Do not "harden" `base_url` with `assert_public_url`**
  — it would be a pure regression. The anti-hardening note now lives at both config sites
  and on `assert_public_url` itself, and
  `test_assert_public_url_rejects_every_normal_integration_base_url` pins the measurement
  so the claim cannot quietly go stale.

- **Cross-origin no-cors GET side effects are an accepted residual.** `GET
  /api/artists/image` (and its peer cache-fillers), plus the outbound-credential GETs like
  `/api/plex/*`, still fire for a foreign page — GETs are structurally outside an
  unsafe-method guard, so the 2026-08-23 slice's "not in this slice" note stands as an
  accepted risk, not an oversight.

- **The image's declared default user is root, and that is an accepted residual.**
  SonarQube `docker:S6471` — the ONLY finding accepted out of the 1,281 cleared in the
  2026-08-25/26 compliance pass, and the reason `Dockerfile` carries no `USER`. The
  serving process is not root: `entrypoint.sh` ends with `exec gosu musicdrop "$@"`, so
  root exists only long enough to remap the `musicdrop` user to the operator's PUID/PGID
  and `chown /data`. The rule is followable — a compliant image was built and verified
  across four deployment scenarios: the `musicdrop` user baked at BUILD time (uid 911) with
  `USER musicdrop`, plus an entrypoint that remaps only when started as root. Note a bare
  `USER musicdrop` would not work at all — nothing creates that user until `entrypoint.sh`
  runs — so the fix is a build-time `useradd`, not one line. This is therefore a cost
  decision, not an impossibility. What carries it: an operator whose PUID differs from that
  baked 911 (unRAID's convention is 99, and `entrypoint.sh` exists precisely because
  operators set it) pulls, restarts without editing their compose, and gets a container
  running as 911 against a `/data` still owned by their old PUID — `Permission denied` on
  start. Breaking a pull-and-restart upgrade for the sake of one compose line was judged
  the worse trade. Revisit if `/data` ownership ever stops being
  operator-set. Full record: the SonarQube issue comment and the vault note
  `musicdrop-sonarqube`; `Dockerfile` carries a pointer at the decision site.

- **Source comments point at gitignored files, so those pointers dangle for every clone.**
  Two families, found by the 2026-08-27 stale-reference sweep. `docs/superpowers/`
  (`.gitignore:51`) is cited 3 times from 3 tracked files, two of them shipped source
  (`app/host_guard.py:30`, `app/playlists/store.py:491`); `CLAUDE.md` (`.gitignore:48`) is
  cited 30 times across 22 tracked files, mostly "CLAUDE.md rule 3" under `app/beets/`.
  Both resolve on the maintainer's machine and nowhere else — the same class as the dead
  commit shas that sweep removed. Left alone deliberately (owner's call, 2026-08-27): the
  ignores are intentional and rewriting 26 files is its own decision. If revisited, the
  cheap fix is to mark each pointer as a local untracked doc rather than to track the
  directory. Related: the two guard design specs under `docs/superpowers/specs/` still
  describe the PRE-#181 middleware order; supersession notes were written locally but
  cannot be committed, because the directory is ignored.

- **A smart playlist that resolves to zero gets no library-path diagnosis, deliberately.**
  `sync.py:657` records `status="failed"` for a smart playlist WITH the real tally (Plex refuses
  to apply track lists to smart playlists; the resolution itself succeeded). The detail page's
  all-zero diagnosis, added in #184, keys on `status == "empty"`, so this case stays silent. That
  is the intended trade: the smart-playlist error is the actionable one, and loosening the
  condition to accept `failed` is exactly the mutation that made a REJECTED push wrongly blame
  the user's library path — the false alarm two regression tests exist to prevent. Consequence
  worth knowing: a smart playlist with BOTH problems only reveals the path issue after the first
  is fixed. Untested in either direction. From the #184 deep review, 2026-08-27.

- **`_make_fetchart_plugin`'s global-config overlay race is a documented residual — now
  documented HERE, not only in its own docstring** (recorded 2026-08-28). The cover-fetch
  path mutates the process-global beets config (`fetchart.set({"auto": False})`) and
  restores it in a `finally`; a concurrent config Apply clearing `beets.config` between
  the two would leave a stale `fetchart.auto` overlay for the process lifetime. The
  docstring at `app/beets/cover.py:128-131` names the hole, labels it a documented
  residual, and names the eventual fix (removing the persistent overlay). Genuinely
  narrow: single user, requires an Apply mid-fetch. Recorded so "documented" is true for
  someone who has not read that function.

- **Wire-safety net coverage caveats** (by design, recorded so nobody assumes otherwise):
  SSE `/api/events` bypasses the response class (scopes are tag-derived today, never paths);
  any future route-level `response_class=` or hand-built `JSONResponse` bypasses both halves
  of the net.

## Open questions

- **Should duplicates resolve / resolve-all gain 503 parity with the delete routes?**
  (#189, owner call.) Both currently keep their established structured-500 absorb shape
  with the honest root-unavailable message embedded — consistent with their other
  failures, but a different status than the same cause gets on delete. Parity is a small
  contract change (two routes' `responses=` + regen); the current shape is defensible.


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

- **From the 2026-08-25 #143-Minors triage** (all re-verified at v0.44.0; per-item evidence
  in the vault note `plex-143-review-minors`): `PlexSettingsPanel`'s `pathParts`/`pathInside`
  resolve no `.`/`..`, so a path that climbs back OUT of a reported folder gets the
  all-clear (the backend's normpath rebases it elsewhere) and one that climbs back IN
  false-warns; a saved section title differing from Plex's only by case shows the same
  library twice in the dropdown (the fix must normalize the select value to the fetched
  spelling — a case-insensitive `includes` alone breaks selection); the mismatch verdict
  recomputes per keystroke inside a polite live region (churns AT and accuses a correct
  path mid-typing — needs settled-value gating; M, several tests type-then-assert
  synchronously today); the nothing-matched-by-file warning names only the library-path
  cause, though after a reorganize the true cause can be "Plex hasn't rescanned yet";
  `_name_words` keeps digits belonging to a script it discarded ("Би-2 (Bi-2)"
  false-vetoes — safe direction, grey row); one shared noise word ("feat") forces
  full-tuple equality and vetoes the transliteration population the album rung exists for
  (loosening a safety veto — needs its own adversarial pass, never a drive-by); `_norm`
  applies no Unicode normalization while `_name_words` in the same file does NFKD, so NFD
  vs NFC metadata never collides in the resolve indexes; claim order is playlist order,
  not match quality (a lone artist+title candidate locks a track away from a later exact
  album match — worth a cross-row preference only if it ever bites live); and the "ONE
  scan of the section" perf invariant is unpinned (`FakeSection.searchTracks` counts
  nothing — three full-library pulls per sync would pass green).

- The playlist-artwork panel's success line may never be announced — and the old entry's
  quotes all grep to zero, which reads as "already fixed" (corrected 2026-08-28):
  `ArtworkEditPanel` is not a file but an inner function of `PlaylistDetailPage.tsx`
  (declared `:1256`), and the region is an HTML `<output>` (implicit role status), so a
  `role="status"` grep misses it. The defect is real and is WCAG 4.1.3 (AA), not
  cosmetic: the region is conditionally mounted WITH its text and double-guarded
  (`outcome !== null && !busy`, `:1359-1360`) — the exact pattern the repo's a11y
  doctrine 600 lines up forbids (`:730-731`), with the correct always-mounted pattern
  (zero-width-space reannounce) ready to copy at `:958`.
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
- ~~No CI guard that `frontend/openapi.json` matches the live spec~~ — **FIXED in #158
  (merged 2026-08-25, `f7e8d0a`; this entry said "awaiting merge" until 2026-08-28).** Guards BOTH links: backend pytest pins live spec →
  `openapi.json` (parsed dicts; missing/corrupt file fails loudly with the regen steps), a
  frontend CI step pins `openapi.json` → `schema.d.ts` (`gen:api` + `git diff --exit-code`),
  and `scripts/dump_openapi.py` is the now-committed step-1 command, byte-pinned by its own
  test. Operational note: a FastAPI bump can change the spec (0.128 did — three Body_*
  schemas), so Dependabot backend PRs may now legitimately go red until someone runs the
  two-step regen; that is the guard working, not a flake.
- ~~`download_image` validates only the FIRST and LAST redirect hop~~ — **moved to Open
  bugs 2026-08-28**: a blind SSRF does not belong under a heading that says "cosmetic /
  self-healing", and the in-code comment asserts a protection the code does not provide.
- ~~`ArtistImageEditPanel.onPickFile` no longer clears notices~~ — the claim was FALSE
  (2026-08-28): `clearNotices()` is the first statement of `onPickFile`
  (`ArtistImageEditPanel.tsx:149-150`); a reader taking the entry literally would "fix"
  code that is already right. The real gap is COVERAGE: the panel has four clear-notices
  entry points and the test at `ArtistImageEditPanel.test.tsx:379` exercises two (Reset,
  Set-from-URL) — `onPickFile` and `onFetch` are unpinned. Fix = third and fourth arms on
  that existing two-arm test.
- `aside.w-96` overflows a 390 px viewport by **42 px, not 18** (corrected 2026-08-28:
  390 − 2×24 of `px-6` = 342 px available for a 384 px rail; 18 was never derivable from
  the entry's own premise) — and `AlbumDetailPage.tsx:158` carries the byte-identical
  rail, where the loading skeleton is ALREADY bounded (`w-96 max-w-full`, `:554`): on a
  phone the album page renders correctly while loading, then jumps to a horizontal
  scroll when content lands. Fix is one token per site — `w-96 max-w-full`, the in-repo
  idiom — not the previously recorded three-utility `w-full max-w-96 lg:w-96`. Needs a
  browser check, not a test: jsdom cannot measure layout.
- The aria-hidden clickable-name idiom now has TWO instances (`MergePlaylistDialog.tsx`,
  `ImportPlaylistsPage.tsx` since the backlog-minors wave): a third should become a shared
  component. Reviews of it should also check `select-none` isn't suppressing selection of
  user data someone might want to copy — on the import rows the playlist NAME is now
  unselectable, an accepted trade-off of the pattern.
- `SegmentedControl` segments are 28 px tall — which PASSES WCAG 2.2 SC 2.5.8 Target Size
  Minimum (24 px, Level AA) and misses only the AAA/platform guidance (44 px Apple HIG,
  48 dp Material): a house-quality call, not a compliance gap (reframed 2026-08-28). The
  same decision covers THREE shared controls, not one: the segments, plus
  `AlphabetIndex`'s 26-letter jump bar and Pagination's numbered page buttons, both at
  32 px (`size="icon-sm"` → `size-8`). `py-1` → `py-2` reaches exactly 36 px (group
  42 px). Fold the `AlphabetIndex` missing `shadow-xs` (already recorded below) into the
  same touch. Load-bearing on mobile: the segments are the image-source picker at
  `ArtistImageEditPanel.tsx:278`.
- Artist-image panel minors, all shipped deliberately: Fetch is `secondary` while the pasted-link
  Set is the only filled control (ranking reads backwards); the URL input's `aria-label`
  shadows its visible label — a genuine SC 2.5.3 Label-in-Name failure (Level A: name
  "Image URL", visible "…or paste an image link", zero overlap, so a speech-input user
  saying the visible label hits nothing), and CHEAPER than recorded (corrected 2026-08-28:
  the `getByLabelText` dependency is ONE file, three call sites, all case-insensitive
  regexes — `ArtistImageEditPanel.test.tsx:199/:410/:429` — not two files); ~~generic
  `alt="Artist image preview"`~~ (that string never existed — greps to zero, reading as
  fixed; the real attribute is `alt="Pending artist portrait"` at `:400`, which already
  names the pending state — residual nit only: it names neither artist nor source, and
  the caption below carries the source); comparing two sources costs a Discard; and a
  possible live-region/focus contention that needs a real screen reader to settle.
- `test_default_settings_disabled_endpoint_404s` proves the 404, not the REASON: with the
  flag mutated to enabled it still passes offline (the inline-grace path also 404s), so
  "no outbound call when off" is asserted nowhere. A bare `@respx.mock` does NOT close
  this — tried and refuted by its own mutant on the backlog-minors wave: the service
  catches source exceptions as transient failures, so an unmocked-call raise is laundered
  into the same 404. A real proof needs a transport spy plus deterministic background-fill
  settling; not worth it until the wiring changes.
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
- ~~Pagination numbered-window `aria-label="Page N"` vs the visible "N" as a WCAG 2.5.3
  mismatch~~ — NOT a violation (2026-08-28): SC 2.5.3 requires the accessible name to
  CONTAIN the visible text, and "Page 3" contains "3"; this is the ARIA APG pagination
  pattern verbatim. At most a best-practice note (visible text sits at the end of the
  name). The icon-sm caret width staying tight for 3-digit page numbers stands.
- Pagination's stale docstring is NOT the click-to-edit one (corrected 2026-08-28) —
  `PageJumpInput`'s is accurate; the stale one is the top-level `Pagination` docstring
  (`Pagination.tsx:35-37`), which promises "deliberately NO scrolling or focus management
  here" directly above a 30-line `useEffect` doing deferred focus restoration with two
  race guards, and describes the interactive compact readout as a passive "terse 2 / 67
  readout". Fixing the entry as previously written would edit the correct docstring and
  leave the wrong one. There's also a minor spinner style delta from the original design. (The busy-state-not-in-the-accessible-name and
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
- ~~413 responses carry no CORS headers (dev-only annoyance)~~ — dead since #181 moved
  CORSMiddleware outermost (2026-08-28 check): the 413 now travels out through CORS and
  is stamped, pinned by three tests in `test_body_limit.py:92-117` (allowed origin,
  disallowed origin, no-Origin). One of the tests' docstrings names the invariant the
  deleted header echo used to serve, so nobody should re-add it.
- PlaylistDetail stale-snapshot move-PUT (self-heals on refetch).
- Remove-to-empty stale focus; playlist-import focus-after-resolve.
- Trash restore leaves `.lrc`/`.txt` sidecars behind in Trash (v1 limitation, noted in #67).
- Trash restore leaves the emptied source folder behind as a 0-track husk row in the listing
  (name-independent; beets moves the files but never prunes the dir — Empty clears it).
  Since #189 a zero-track row keeps Restore ENABLED with an honest uncertainty note
  (zero tracks = unreadable tags, not no audio); a genuinely media-free row still cannot
  restore — origin-recording at trash time is the recorded follow-up under Open bugs.
- The hand-rolled banner idiom has **seven** instances across five files, not one
  (corrected 2026-08-28; the old entry named only MoveNotice) — two complete banner
  recipes ship side by side: StatusBanner (`rounded-xl gap-3 size-5 items-center p-3`)
  vs the legacy recipe (`rounded-md gap-2 size-4 items-start`, itself fragmented `p-3`
  vs `p-2`), and `PlaylistDetailPage.tsx` renders BOTH on one screen. The migration is
  NOT mechanical: every legacy site uses `items-start` with a nudged icon because its
  copy wraps to 2-3 lines, while StatusBanner is `items-center` — migrating as-is drops
  the icon ~20 px below the first line. One design call first (an `align` prop, or
  change StatusBanner's base to `items-start`), then seven mechanical replacements:
  `AlbumEditPanel.tsx:308` (MoveNotice) and `:432`, `ArtistImageEditPanel.tsx:428`,
  `CoverEditPanel.tsx:226`, `PlexSettingsPanel.tsx:353` and `:399`,
  `PlaylistDetailPage.tsx:338`.
- Unfocusable scroll containers: **four, not two** (corrected 2026-08-28), and the two
  previously unrecorded are the biggest and carry no `aria-label` either — the reorganize
  plan's MAIN moves list (`ReorganizeControl.tsx:188`, `max-h-72`, potentially hundreds
  of rows) and the orphans list (`:206`, `max-h-40`), alongside the two already named
  (ConflictList `:86` and MoveRefusalList `AlbumEditPanel.tsx:354`, both labelled).
  Safari keyboard users cannot scroll any of them — WCAG 2.1.1 (Level A), visible and
  unreachable, not cosmetic; mis-filed in this section. Fix is one `tabIndex={0}` plus a
  label per container; the `tabIndex={-1}` at `:416`/`:530` is on the plan WRAPPER (a
  programmatic focus target) and does not close this. Fix all four together.
- Trash/inbox rows key on the scrubbed display name (`key={album.folder}` /
  `key={item.name}`), so two differently-damaged non-UTF-8 siblings share a React key
  (duplicate-key warning, possible node reuse). Cosmetic — either row's action 409s cleanly.
- `empty_trash_one` resolves the display name outside the swap lock (`restore` resolves
  inside it); the placeholder scandir path widens that pre-existing TOCTOU window slightly.

Added by the 2026-08-28 sweeps:

- `GET /api/reorganize/preview` is ungated (`raise_if_library_busy` sits only on the
  POST) and does a whole-library `os.walk` — and, since #189, one `lib.items()` pass +
  one `destination()` render per album for the protected set. The walk still dominates;
  the ungated preview is the pattern to fix, not the new pass. (#189 security note.)

- Browse filter-rail counts are whole-library totals while a filter is active — deferred
  outright in the adapter docstring (`browse.py:430`: "Absolute counts (not filter-aware)
  — a drill-down refinement is deferred") and repeated at `BrowsePage.tsx:61-62`; the
  rail renders `{fv.count}` beside each checkbox, so with `genre=Rock` active the Media
  facet still advertises the whole library's Vinyl total, and ticking it returns a
  fraction of the promised number — reads as a filtering bug, is a labelling choice. Was
  in two docstrings and on no board.
- The shipped starter-config header makes two false claims that become the header of the
  USER'S own file on first run (`config.starter.yaml:2-3`): "MusicDrop reads it; it never
  writes back" (the config editor's save and save-naming both `atomic_write` it) and
  "Edit and restart MusicDrop to apply changes" (`POST /api/config/apply` re-arms beets
  in-process). The CAS 409 catches concurrent hand-edits, so this is confusion, not
  corruption.
- `_sweep_orphans` swallows a failed husk move (`runner.py:130-133`, `except OSError:
  continue`) — skips the orphan tally and joins no failure list, so a permission-denied
  or cross-device move is invisible: the job finishes `done` and the preview lists the
  same folder next run with no explanation. The orphan phase is the runner's only phase
  with no failure channel.
- 12 cache-degradation tests self-skip silently when the suite runs as root
  (`pytest.skip("running as root…")` — 8 in `test_artwork_cache.py`, plus
  `test_artist_image_endpoint.py:632`, `test_cover_thumbs.py:141`, and two "needs a
  non-root user" guards in `test_acquisition_inbox.py`). They are exactly the tests
  around the open artist-image-cache bug, and the shipped image's declared user is root —
  a maintainer running the suite in-container gets a green with these unexercised and no
  summary line saying so. CI runs non-root, so CI is unaffected.
- One production `# type: ignore` lacks its same-line reason
  (`app/models/import_models.py:390`; the reason exists two lines above) — the sole
  literal miss of hard rule 1's comment requirement, and nothing enforces that rule
  either way.
- Eleven `@theme inline` aliases generate utilities nothing uses: the eight-token sidebar
  family (`styles.css:151-158`, whose "wired to the real sidebar in Phase 2" comment at
  `:69` was overtaken — the real Sidebar uses the elevation ramp), plus
  `destructive-foreground`, `success-foreground`, `warning-foreground`; `surface-base`
  and `error` have live `:root` vars but dead aliases. The reverse direction from the
  unknown-utility bug — tokens without classes, in a file whose stated principle is "no
  dead CSS". Also stale: the header at `styles.css:26-28` promises a Phase 3 sweep of
  leftover `dark:` utilities; zero remain (the only two `dark` hits are a CodeMirror JS
  option, not utilities).

## Recently shipped

- **SonarQube compliance programme — 1,281 issues to 0, shipped 2026-08-25/26 across 22 PRs
  (#160-#181).** Recorded as ONE entry because the wave structure, not the individual PRs, is
  what a future reader needs; per-PR detail is in the git log and the vault note
  `musicdrop-sonarqube`. Quality gate OK; the zero was AUDITED rather than trusted — analysis
  revision matched `main`, the project carries a single branch so no feature scan could have
  overwritten it, quality profiles were stock and unedited, and `sonar-project.properties` has
  one commit in its entire history, so no exclusion was ever widened. **1,280 fixed, 1 accepted,
  0 classified false-positive** — the owner's standing rule is that a true finding is never
  filed as a false positive.
  - *Wave 1 (#160-#163)* — Phosphor `*Icon` exports and current React event/ref types; composite
    assertions split (PT018 selected); component props `Readonly<>`; `response_model=` kwargs
    dropped where the return annotation already says it (regenerated contract byte-identical).
  - *Wave 2 (#164-#167)* — test hygiene: one raising invocation per `pytest.raises` (PT012),
    `monkeypatch` over hand-rolled save/restore (PT001), `findBy` replacing `waitFor`+`getBy`,
    specific matchers and documented jsdom stubs.
  - *Wave 3 (#168-#174)* — a `plural()` helper un-nesting count ternaries; derived render states
    across shell, settings, import, review, playlists, browse, artists and search; and three
    complexity passes (`beets` adapter, service layer, import review loop) that brought every
    module under the threshold by extracting named phases.
  - *Wave 4 (#175-#181)* — security (`usedforsecurity=False` on a non-security hash, writes
    adopting the neighbour's mode via `copymode`, loopback placeholders), the remaining smells,
    a TS idiom sweep, three a11y PRs (native `<output>` live regions, real semantics on
    interactive controls, vacuous pins replaced), and the S8415 error-status sweep.
  - **Lock-on-clear:** each family driven to zero also gained its lint twin in the SAME PR,
    mutation-tested — ruff `select` now carries PT018, PT012, PT001 and S324. The `S2612` chmod
    family has NO ruff twin (S103's threshold ignores o+r bits), so the server scan is its only
    net. Full lessons: auto-memory `sonar-lessons-standing` and `sonar-wave4-lessons`.
  - **The one accepted finding is `docker:S6471`** (the image's declared default user is root) —
    now recorded in `Dockerfile` and under Accepted residuals above, so it no longer lives only in the
    vault. It was declined on a reproduced upgrade cost, NOT on the rule being wrong.
  - **A green gate is not contract completeness.** S8415 read 0 while `POST /api/import` still
    had no 409 in the contract, because the rule matched integer literals only and that status
    was written `status.HTTP_409_CONFLICT`. #181 replaced the audit with a test that resolves the
    constant spelling and checks the LIVE spec, and it found 39 further gaps. Do not read a
    zeroed rule as proof of the property it approximates.

- **Stale-reference sweep — #183, shipped 2026-08-27.** Three classes of reference that resolved
  on one machine only: six dead commit shas (branch-local commits destroyed by their own
  squash-merge — one was gone from the repository entirely), two comments still calling the host
  guard "outermost" after #181 moved CORS outermost, and `library.py` claiming to be the only
  module importing beets when 25 do (the boundary is the PACKAGE). Added
  `tests/test_cited_shas.py`, which resolves every sha cited in a comment against `origin/main`
  — not `HEAD`, because a branch-local sha IS an ancestor of its own branch and a HEAD check
  would redden the wrong PR. CI gained `fetch-depth: 0` for it.

- **#143 playlist-mapping triage — #184, shipped 2026-08-27.** A claim-blocked row reported
  `not_found`, whose tooltip sent the user to check a file that was never the problem; it now
  reports `claimed_by_other_track`, returned only where the claims are what emptied a pool of
  candidates that would genuinely have matched. An all-zero-but-real tally rendered nothing while
  a PARTIAL miss got a warning box — the loudest failure was the quietest — now diagnosed by
  discriminating on status plus tally-nullability. And the artist veto could be switched OFF by a
  Unicode width, since fullwidth Latin shares no "script" with ASCII; both names are NFKC-folded
  before the scripts are read. That last one failed UNSAFE, accepting a silent wrong match.
  An isolated mutation review caught a real correctness bug in the first attempt while CI was
  fully green — see auto-memory `coverage-regressions-and-diagnosis-claims`.
  - **A claim-blocked row reported `not_found`, and the tooltip lied.** When another row's claim
    emptied the candidate pool, `mapping.py` returned `not_found` (`MissReason` had no third
    value) and the tooltip told the user "Plex has no track with this file… Check the file is in
    your Plex library" — false: Plex had it, a sibling row took it. #146's `duplicate_collapsed`
    covers only the sync-level collapse, not this resolution-level block. Closed as the
    wire-contract change it needed: the new `MissReason`/`PlexMissReason` value, returned by BOTH
    fallback rungs, the regenerated contract, and its own tooltip copy (the badge stays "Not in
    Plex" — a lookalike is on Plex, this track still is not).
  - **An `empty`-status sync with a real all-zero tally got NO diagnosis.** The playlist detail
    page's `resolvedTargetState` required a non-zero tally sum, so the loudest wrong-path case
    (every lookup missed; tally recorded all-zero with misses > 0) rendered no match summary and
    no library-path pointer, while a PARTIAL miss got a warning box. A second pass now accepts a
    tally that EXISTS on a target whose status is `empty`; legacy records (null tally) and failed
    targets stay silent as before, and the case is pinned by a test with `missing > 0`.
  - **`_shared_scripts` read raw characters, so fullwidth Latin disabled the artist veto.**
    Per-character scripts were derived before any compatibility folding, so an ASCII vs
    fullwidth-Latin pair shared no script, `_artist_contradicts` could not compare the names, and
    the album rung accepted — a silent wrong match, the direction the module's own contract
    forbids. Both names are now folded before the scripts are read — NFKC rather than the NFKD
    the open entry proposed, because the same folded text then feeds `_name_words`' letter
    filter, so a name cannot collapse to empty and compare equal to everything.

- **Stale comment-claims cluster — #185, shipped 2026-08-27.** The six factual claims the #183
  sweep recorded as "found but NOT fixed" are closed. Every one was replaced by a predicate a
  reader can check with one grep, never by a corrected number that would simply re-stale.
  - Three comments (`app/api/albums.py`, `app/api/artists.py`, `app/api/import_.py`) pinned a
    defence-in-depth completeness argument to "`header_safe_content_type`'s docstring
    enumerates/counts the sinks" — a register that docstring has never held (it enumerates
    PROVENANCE and FAILURE SHAPES). The argument is kept verbatim and only the appeal goes,
    deliberately NOT by writing the register: a hand-maintained list of sinks is exactly the
    artefact that goes stale in silence. What replaces it is the invariant itself — an external
    content-type reaching a response header must pass through `header_safe_content_type`, so
    grep the name and every site is visible. `artists.py` also called itself "the third
    content-type sink" (no ordering makes it third) and claimed to be the only site reading a
    source's OWN answer rather than a cache sidecar, which the earlier call in the same handler
    already refuted; both claims are gone with no number in their place.
  - `app/api/disk_sync.py` said "the same 8-gate set" — a number matching neither the job types
    nor the call sites. It now names the shared `app.library_busy` union and its `_gate_busy`.
  - `app/api/http_cache.py` said `GET /api/artists/image` "alone has six exits"; it now says the
    exits do not fit on one screen, which stays true across the next extraction.
  - `app/api/import_.py` said "the fourth and last image response in the app"; it now names the
    predicate — the only image response reaching neither the `http_cache` constructors nor the
    artwork routes. `albums.py` had claimed the same "last" title, so the two contradicted each
    other outright; that claim went with them.

- **#143-Minors triage fix slice — shipped 2026-08-25 (PR #157 = `3ae7d92`).** The
  fix-now portion of the banked-Minors adjudication (see Next up). Copy honesty: the
  settings mismatch warning now describes the real fallback ladder (artist and title,
  then album, title and length) and the different-copy risk, instead of citing the case
  the third rung absorbs; the playlist match summary anchors to "Last sync matched…",
  names the weakest rung "by album, title and length" (it keys on album + title +
  duration), and the nothing-by-file box ends with the re-test action. A11y: sync
  outcomes re-announce to assistive tech even when byte-identical — an invisible
  zero-width-space token varies per announcement; plain `setStatusMsg` bailed in React
  and the "still broken" step of the repair loop was silent. Prose alignment: the
  smart-playlist tally exception is stated at the three comment sites that used to state
  the unqualified all-zero rule; `mapping.py`'s docstring scopes the one-track-one-row
  claim to the fallback rungs. Seven new test pins (digit-veto pair, settings
  fill-on-exact-equality, relative-vs-absolute mismatch, section-title
  case-insensitivity, mixed-tally announcement, singular tally, announcement re-fire;
  eight test functions, the digit-veto pair counting two) — the deep review ran 12
  mutants across them (incl. its own three vacuity probes: constant token, ZWSP→plain
  space, conditional region mount), every one killed.
  Browser-verified on all three UI surfaces with fixture interception. The UI review's
  two Importants were adopted in-branch: tally clauses joined with semicolons (the album
  rung's name carries its own comma — "22 by artist and title, 6 by album, title and
  length" reads as four buckets, worst aloud), and the per-row miss tooltips no longer
  teach the one-rung ladder ("nothing matched by its tags"). Behavior changes: none
  beyond the re-announce token. Accepted residuals: the ZWSP re-announce is proven at the
  DOM level (jsdom + real Chromium) but no real screen reader has heard it — one NVDA/
  VoiceOver pass of the repeat-sync loop is still owed; JAWS's own dedupe of identical
  consecutive utterances can still eat a rapid repeat regardless of DOM state; ≥4
  identical announcements batched into ONE React commit would collide on the token cycle
  (unreachable on this page today; deep-review-confirmed on a replica — the airtight fix,
  if ever wanted, is comparing against the previously-rendered token in a ref, since no
  modulus survives an N-batch); "nothing matched by its tags" also covers a blank-
  albumartist row the album rung REFUSED to compare (deliberate rung exclusion — the old
  copy had the same gap and named fewer rungs); and the panel's `resolveSection` is
  `toLowerCase` against the backend's `casefold`, so a ß-class section title ("Straße"
  saved as "STRASSE") resolves on the server but shows no folders in the panel
  (pre-existing, now noted at the function and scoped "(ASCII)" in its test name).

- **Artist rename — shipped 2026-08-24 (PR #156 = `e854871`).** One action on the artist
  page that fans the existing album edit across every album of the artist: `album_artist`
  only (per-track artists never follow — lyrics fetching keys on them), merges onto an
  existing name are the primary use case, synchronous apply with per-album outcomes
  (drift-skip, one failure never aborts the batch), portrait-cache re-key (a manual pin
  outranks a bare auto image on merge), best-effort artist-art job kick and `.m3u8`
  re-export for moved tracks. Browser-verified end-to-end (8-album rename + a merge) on a
  throwaway library. Accepted residuals, in rough priority order:
  - The old audio-empty artist folder (possibly holding stale `artist-poster.*`) is left for
    the library-scope reorganize orphan sweep.
  - A GET-driven artist-image fill for the OLD name can interleave with the cache re-key and
    recreate old-key slot files (leftover bytes only; a fix needs a cache tombstone/epoch).
  - `PlexMissingTrack.albumartist` snapshots keep the old spelling until the next sync
    (self-healing); the `["artist-image","sources",name]` FE query still has no event
    invalidation (pre-existing).
  - `detailMessage` (frontend `lib.ts`) drops the `recovery` half of structured 500 details
    repo-wide — the actionable "albums already renamed keep the new name" line never reaches
    the user on a mid-batch abort.
  - `StatusBanner`'s `action` slot renders inside the alert live region, so an action
    button's label is announced as alert text (component-level fix).
  - The rename routes declare no 404/409 in `responses=` while the artist-art routes declare
    their 409 — the `responses=` convention is split and worth settling once.
  - The busy-path exception contract (`RuntimeError` from `claim_slot`) is pinned only via
    stubs that mirror the type; slot-layout writers still use scattered literals (the
    `_ALL_SLOT_SUFFIXES` relationship test ties constants, not writers).
  - Rename/edit/delete/resolve-all QUEUE on a held swap lock rather than 409 (sibling
    semantics, judged deliberate); a uniform 409 would be a five-site slice.
  - UI polish queue: surface `portrait`/collateral outcomes (a `not_rekeyed` landing shows a
    missing portrait with no explanation), a success toast, retry labeling after partial
    failure, the older dialogs' plain-`disabled` buttons vs the new aria-disabled doctrine,
    and a typographic-twin warning on near-invisible rename targets (reuse the import gate's).

- **Security response headers — shipped 2026-08-24 (PR #155 = `653c26c`).**
  Pure-ASGI `SecurityHeadersMiddleware`, added after the three guards so it wraps
  outside all of them (CORS is outermost, added last for python:S8414): five
  headers on every response it passes through (`nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`,
  `Cross-Origin-Resource-Policy: same-origin` — closes the no-CORS `<img>` library-existence
  oracle the security audit found — and a CSP). Strict CSP everywhere (`script-src 'self'`;
  inline styles allowed for shadcn/Radix; `img-src` broad for user-pasted image previews);
  `/docs`, `/docs/oauth2-redirect`, `/redoc` alone get a docs-relaxed policy for the
  Swagger/ReDoc CDN assets — exact path match, fail-closed. Browser-verified violation-free
  (SPA + both docs pages) twice independently. Accepted residuals, recorded: the docs policy
  is a union over the three pages (each page individually over-granted; nothing exploitable
  — no injection sink on any of them); the docs pages load floating-major vendor bundles
  from cdn.jsdelivr.net, so a CDN compromise runs same-origin script for a `/docs` visitor
  (tightening path if ever needed: self-host the bundles via custom docs routes); Starlette's
  synthesized 500 and uvicorn's protocol-level 400 carry no headers (fixed bodies — and a
  global `Exception` handler would move real 500s OUT of header reach; don't add one).
  No HSTS by design (TLS terminates at Caddy; plain-HTTP LAN access exists).

- **Host allowlist (DNS-rebinding guard) — shipped 2026-08-23 (PR #154 = `f5086f9`).** All-method
  `HostGuardMiddleware` (wraps outside the body-limit and origin guards, below the
  security-headers stamper and CORS): Host / X-Forwarded-Host must be a bare IP literal,
  `localhost`, or a name in `MUSICDROP_ALLOWED_HOSTS` (exact, case/port-insensitive match, no
  wildcards); everything else 400s. Dev posture additionally allows `testserver` (prod-pinned
  not to). Startup now logs the effective security posture (closes the silent-`static_dir`
  minor). Deploy delta: browsing via a DNS name (e.g. the Caddy site) requires
  `MUSICDROP_ALLOWED_HOSTS=<that name>`; by-IP access and the container healthcheck are
  unaffected. Residuals: real auth is still the stronger long-term control (separate item);
  wildcard entries deliberately unsupported until a deployment needs them.
  Spec: `docs/superpowers/specs/2026-08-23-host-guard-design.md`.

- **App-wide origin guard (PR #153, 2026-08-23).** Cross-origin browser writes are
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
