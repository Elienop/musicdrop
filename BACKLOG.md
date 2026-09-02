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

1. ~~**The data-safety slice**~~ — **SHIPPED on `fix/undoable-deletes`** (2026-08-31): the
   unmounted-share ghost delete and the unrestorable Trash rows, both closed below, plus the
   README and `decisions.md` 27 (amended). Original entry kept for its correction record.

   **The data-safety slice — now TWO findings, not three.** The 2026-08-28 data-loss findings
   that destroy user files or rows with no confirmation and no in-app recovery. Its *critical*
   member, the lyrics-backfill sidecar deletion, **shipped in #189** and is struck below; this
   item still described it as open and pointed at it as "first entry below", a cross-reference
   that stopped resolving when other entries closed above it. Corrected 2026-08-31 — re-derive
   before picking this up, do not trust this sentence either. What remains, both open, both in
   the beets adapter and both on the delete path:
   * **the unmounted-share ghost delete** — the delete-path mount predicate accepts a root with
     ANY entry, so a stray file on an unmounted share reads as "mounted" and the delete lands
     in the mountpoint instead of the library;
   * **the Trash rows Restore can never restore** — no origin path is recorded at trash time,
     so a move-back restore is impossible (with the orphan-sweep feeder that fills those rows).
   They are one slice because they are the same code path and the same failure mode: a delete
   that cannot be undone.
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
4. ~~**Real authentication**~~ — **DONE 2026-08-30. All three slices of option C shipped**
   (#199 = `3e82ae4` = v0.46.0, #200 = `0eca9c2` = v0.47.0, #201 = `9d1b4e7` = v0.47.1).
   The full record moved to *Recently shipped* below; what remains from this item is not the
   item but the findings it produced, each filed separately under *Open bugs / hardening*.

The 40 banked #143 Plex review Minors stay fully adjudicated (2026-08-25, every item
re-verified against v0.44.0): 12 shipped as the triage fix slice (see Recently shipped), 12
recorded below, 3 accepted as deliberate, 3 were already fixed. Of the 12 recorded, the
three that sat under Open bugs shipped in #184; the nine under Deferred minors remain open.
Dispositions with per-item evidence: the vault note `plex-143-review-minors`.

## Open bugs / hardening

- ~~**`tests/test_import_session.py::test_attended_astracks_lands_the_singletons_full_pipeline`
  writes to the developer's PERSONAL beets config dir**~~ — **FIXED on branch
  `fix/test-suite-beets-dir-isolation`, 2026-08-31.** (No sha cited: a branch-local one is
  destroyed by the squash-merge, which `tests/test_cited_shas.py` caught when this note first
  tried it.)
  **This entry's measured blast radius was UNDERSTATED, and the reason is worth keeping.** It
  read "bounded: only beets' importer scratch state is written, the personal `library.db` md5
  is unchanged and no `.bak` appears". That is true only on a machine with **no personal
  `~/.config/beets/config.yaml`** — which is what every measurement so far happened to be taken
  on. Re-measured 2026-08-31 by running the pre-fix suite under a throwaway `HOME`:
  * no personal config → **1** file written (`state.pickle`), suite green;
  * personal config present → **14** files written, including a real `library.db` and
    **eleven** `library.db-before-*.bak` schema-migration backups, AND a red suite
    (`tests/test_albums.py::test_lifespan_opens_library_from_settings` fails).
  Those `.bak` files are beets running pending migrations against a real personal library —
  the same accident as the 2026-08-15 `beet --version` incident cited below. Since MusicDrop
  is a beets web UI, the "personal config present" case is the NORMAL one for a contributor,
  so the damaging configuration was the one nobody had measured.
  **The fix is a `BEETSDIR` floor**: a new ROOT conftest (`backend/conftest.py`, set at import
  so no later import can out-order it) pointing at one throwaway dir per pytest process, plus
  three tests that pin it. Session-scoped is correct, not a shortcut — confuse's `config_dir()`
  memoises nothing and re-reads the environment on every call, so one env var set once suffices
  and is immune to fixture ordering. Measured: only `--noconftest` and `--confcutdir` defeat it;
  `--rootdir`, `-c`, cwd changes, path args and `pytest-xdist` all keep it live.
  **Why a floor and not a per-test fix** (the shape this entry originally proposed, and the
  shape SpenDrop uses): "the only offender" holds only on a machine with no beets config.
  Otherwise at least two tests reach the platform dir and *which* ones depends on machine
  state. This class has already been patched per-test twice — the two
  `test_artist_image_endpoint.py` lifespan tests, via `_pin_settings_at` — and grew back. The
  floor is O(1). Per-test isolation stays the default everywhere else (~2,600 `tmp_path` uses).
  **An autouse write-detector was built alongside it and then CUT**, on the owner's challenge
  that it was over-engineering. A deep review agreed with the owner and supplied the evidence:
  7 mutations of the detector survived the full 2810-test suite (including `autouse=False`,
  `assert after == after`, and an empty watch-list), it crashed the whole session on a dangling
  symlink because `entry.stat()` sat outside its `except OSError`, it compared only one side of
  a symlink-normalised membership test, and it was blind to session-scoped, import-time and
  subdirectory writes — the very routes it was meant to back up. Recorded so it is not rebuilt:
  a second mechanism that nothing pins is not defence in depth.
  **Consequence elsewhere:** `make coverage` is now safe locally, so `SONAR_SKIP_COVERAGE=1` is
  no longer needed *for safety* — the vault rules note still says it is, correctly for `main`,
  and must be corrected when this merges.
  *(Original entry below, kept because its history and bisection are the record — but read its
  blast-radius claim against the re-measurement above.)*
  *(historic detail, FIXED — kept for the bisection record)* The same test wrote to
  `~/.config/beets/state.pickle` on every full-suite run — bisected as the only offender, and present at least as far back as `643783f`,
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
  **The slskd twin shares the false-success half** — a first draft of this entry called it
  "already correct", which is true only of the crash half. Measured: a 20,000-character junk
  body comes back as `ok=True` with the junk as `version`. So the "refuse `ok=True` without a
  parseable identity" fix belongs on both sides. Where slskd genuinely is correct is not
  crashing —
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
  (`app/import_jobs/runner.py:99`), and it checks exactly one thing — an *explicit* copy of
  an in-library source. **A root path passes it in EVERY mode, copy included**, which a
  first draft of this entry got wrong by saying "in the default mode" and so implying copy
  was covered. Measured 2026-08-30: `validate(["/"], operation="copy")` and
  `validate(["/mnt"], operation="copy")` both **pass**, while a path *inside* the library
  raises `InLibraryCopyError`. The cause is that `is_in_library_source` tests only
  source-at-or-below-library, so any **ancestor** of the library defeats it — and the
  worker's "second" guard re-calls the same predicate, so it is one lock counted twice.
  That matters because an operator who picks Copy and types `/` gets exactly the harm the
  guard's own message warns about ("a copy-import would duplicate its files"), at library
  scale, with the guard silent. The model does not help either
  (`app/models/import_api.py::StartImportRequest.path` is constrained to non-empty after
  stripping, and nothing more).
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

- ~~**The frontend has no linter, so the Sonar "lock-on-clear" rule cannot hold there — and
  three cleared families have now measurably regrown (2026-08-30, found while clearing auth
  slice 2's Sonar violations).**~~ — **FIXED in #202, 2026-08-30.** The owner's standing
  instruction is that the PR driving a family to zero also enables its lint twin, so CI pins
  it; the backend honoured that through ruff `select` (PT018, PT012, PT001, S324) and the
  frontend could not, having no `eslint.config.*`, no eslint dependency and no `lint`
  script. Auth slice 2 then reintroduced three already-cleared families in new code —
  `S9020` ×2, `S6819` ×1, `S1874` ×2 — caught only by the server scan, after review.

  What shipped: `frontend/eslint.config.js` on ESLint 10, **closed by default** (it extends
  `tseslint.configs.base`, which enables zero rules, and allowlists from there — extending
  the recommended presets instead produced 182 findings of which 30 came from rules nobody
  had selected, and that turn-it-off list regrows on every preset update). **19 rules** covering 18
  families this project has actually violated and fixed (S1082 is a union of two). A `lint` script, an
  `ESLint` step in CI ahead of `Typecheck` mirroring the backend's `ruff check` seat, and
  `frontend/eslint.config.test.ts` — 25 mutation cases that feed each rule the smell it
  guards and assert it reddens, plus two coverage tests (27 in all): one fails if a rule is
  enabled without a case **or is set to `"warn"` rather than `"error"`**, the other if the
  MAIN block stops reaching a source directory. Verified by mutating the config seven ways
  — drop the decorator, widen its exemption, drop its custom-component pre-filter, disable a
  rule, downgrade a rule to `warn`, move a rule between scope blocks, and ignore
  `src/pages/**`. Each reddened exactly one test, and every restore went green.

  The `warn` case is worth spelling out, because it was the review's Critical and it is not
  obvious: **`eslint .` exits 0 when only warnings are present.** So a single `"error"` →
  `"warn"` edit disabled the whole gate while the suite stayed green — the smell was still
  reported, so the fixture assertions passed, and the "is this rule enabled" check accepted
  `warn`. Closed from both sides, because they fail at different moments: the script is now
  `eslint . --max-warnings 0` (fails once a violation exists) and the coverage test asserts
  severity is `error` (fails the instant the config is edited). Measured: with a violation
  on disk and the rule at `warn`, bare `eslint .` exits 0 and `--max-warnings 0` exits 1.

  Two structural facts drove the shape, both read off the server rather than assumed.
  **Sonar rules carry a `scope`**, and the scanner cannot raise a MAIN-scope rule in a file
  `sonar.test.inclusions` qualifies as a test — so the config splits MAIN from TEST the same
  way, and excludes `src/components/ui/**` to match `sonar.exclusions:22`. Without that
  split the gate failed on 19 findings Sonar is structurally incapable of reporting. Three
  rules are TEST-scope (`S5906`, `S5976`, `S9020`); the first two were pinned in the MAIN
  block first, where they can never fire on the files Sonar raises them in. **And `S6848` is
  `no-static-element-interactions`, not `no-noninteractive-element-interactions`** — that is
  `S6847`, which this project has never violated (0 issues ever, against 3 for S6848). The
  wrong twin was pinned first, from memory; it fired 3× on code Sonar has no complaint
  about. The mapping is in the sonarjs README, lines 505-506.

  One rule is wrapped rather than raw. `jsx-a11y/prefer-tag-over-role` reports 7 sites that
  Sonar accepts — `role="status"` that is also a live region — and the silence is measured:
  all 7 lines were last touched between 2026-05-27 and 2026-08-21, the 2026-08-30 `main`
  analysis scanned them, and S6819 is at 0 open. Deleting the rule was the obvious response
  and the wrong one, S6819 being both a 45-issue family and one of the three that regrew, so
  it is decorated with that exemption and nothing wider. **An earlier note here claimed
  these 7 were live sites Sonar misses; that was backwards — they are over-fires by the raw
  rule.**

  **The exemption is not inferred — it is transcribed.** `eslint-plugin-sonarjs@4.2.0` ships
  279 rules and does **not** implement `prefer-tag-over-role`; its README merely maps S6819
  to the jsx-a11y rule, which makes the real logic look unreadable. It isn't: it is in the
  analyzer, at `/mnt/data/sonarqube/scanner-cache/*/sonar-javascript-plugin.jar` →
  `sonarjs-1.0.0.tgz` → `package/bin/server.cjs`, minified but greppable by rule name. The
  decorator there reports only when a DOM pre-filter passes and none of **nine** exemptions
  match. Three of those facts are now mirrored in the config and pinned by cases: `Qdr`
  admits only native HTML tag names, so Sonar never raises this on a custom component —
  `<Alert role="status">` is ordinary shadcn code and the raw rule reds the build on it;
  role is read with `getLiteralPropValue` and lowercased, so `role={"STATUS"}` resolves like
  the bare literal; and `U8g` is `role==="status" && !!getProp(attrs,"aria-live")` —
  **presence, not value**. The other six exemptions (slider, radio, combobox, separator,
  img-on-div/span, and two more) are named in the config but deliberately not implemented:
  none occurs in the tree, and copying nine minified predicates would make an untestable
  fork of an analyzer out of a 20-line mirror.

  That last point cost a round trip worth recording. The security audit found the first
  version exempting `aria-live="off"`, which is by definition not a live region, and it was
  right on the accessibility merits — so the fix allowlisted `polite`/`assertive`. Reading
  `U8g` showed that made the gate **stricter than the thing it mirrors**, which is the one
  property this file exists to avoid: it would fail the build on code the server accepts.
  Reverted to presence-only. The a11y argument is real and belongs upstream at Sonar, not
  in a mirror.

  Scoped out at the time: 10 of the 28 cleared frontend families had no twin enabled — nine
  mapping to `eslint-plugin-unicorn` and `eslint-plugin-react`, neither then installed, plus
  `css:S8776`, a CSS rule with no JS twin. **Seven of those nine were pinned in the
  follow-up** (`S7780`, `S7776`, `S7760`, `S6772`, `S6481`, `S6478`, `S1186` — 16 of the 21
  issues), taking the gate to 26 rules over 25 of the 27 JS/TS families. Both plugins are
  pinned to the versions the analyzer itself runs — `eslint-plugin-unicorn@65.0.1` (which
  needs no peer override; it peers `eslint >=9.38.0`) and `eslint-plugin-react@7.37.5`
  (which does, same scoped shape as `jsx-a11y`) — because a newer major is a different
  linter from the one being mirrored.

  **Three residuals, and they are decisions rather than omissions.** `css:S8776` still has
  no JS twin. `S7755` (`unicorn/prefer-at`, 2 issues) and `S6479`
  (`react/no-array-index-key`, 3 issues) are deliberately OFF: both are `decorated` with
  suppressions that cannot be mirrored cheaply, so the raw rules are STRICTER than Sonar,
  and a gate stricter than the server fails CI on code the server passes. S6479 is the
  measured case — raw it reports **9 sites on `main`** that Sonar accepts, every one the
  `` key={`${x.label}-${i}`} `` composite-key idiom that Sonar drops on its `LBg` arm. Both
  reasons are written out in `frontend/eslint.config.js` under "DELIBERATELY OFF"; do not
  re-derive them, and do not enable either rule without porting the suppression first.

  **The gate now lints its own config, which it could not before.**
  `frontend/eslint.config.js` used to resolve to ZERO enabled rules — every block needed the
  typed parser and no tsconfig included a `.js` file at the frontend root — so the one `.js`
  file SonarQube scans sat outside the gate entirely. That was not theoretical: the server
  scan found a `javascript:S1874` in that very file during #202, on the deprecated
  `tseslint.config` call, precisely because the gate could not check itself. Closed with
  `allowJs` plus the file in `tsconfig.node.json` (verified: `tsc -b` does not cascade) and
  one narrowly scoped block. Two families are pinned there, and BOTH were chosen from
  evidence rather than taste: `sonarjs/deprecation` (S1874, the one that escaped) and
  `@typescript-eslint/prefer-optional-chain` (S6582), added after the first branch scan run
  with the file linted reported one in the S1186 mirror. The block stays narrower than the
  MAIN allowlist on purpose — narrower than the server is the safe direction — so widen it
  only when a scan shows a family actually firing here.

  Also unchanged: the 7 `role="status"` sites themselves — converting them to `<output>` is
  a real UI change needing browser verification, and `SlskdPanel.tsx:304` is an
  always-mounted live region that the earlier a11y wave already flagged as needing its own
  thought.

  One self-inflicted hazard is worth recording because it passed every green check.
  `eslint-plugin-jsx-a11y@6.10.2` peers `eslint` at `^9`, and installing it with
  `--legacy-peer-deps` **silently removed `@testing-library/dom`** — a peer of
  `@testing-library/react` — breaking all 118 frontend test files with `Cannot find module`.
  The flag suppresses peer resolution tree-wide, not just for the conflict it was aimed at.
  Replaced with a targeted `overrides: { "eslint-plugin-jsx-a11y": { "eslint": "^10" } }` in
  `frontend/package.json`, which relaxes only that one peer edge — measured: 369 → 578
  lockfile entries, **0 removed and 0 version-changed** across all 369 pre-existing ones,
  0 new advisories, 0 new install scripts, every addition dev-only and from
  `registry.npmjs.org` with a sha512 integrity hash. The range is `^10` rather than
  `$eslint` deliberately: `$eslint` tracks the root spec, so an ESLint 11 bump would keep
  installing a plugin two majors behind in silence. `^10` makes npm raise ERESOLVE instead
  — verified by simulating the bump, which errored rather than quietly nesting a second
  ESLint. `eslint-plugin-jsx-a11y@6.10.2` is the latest release and is 672 days old, so
  that re-raised conflict is the signal to check whether the plugin is still alive.

  Two more from the same audit, both fixed here. `.github/workflows/ci.yml` had **no
  `permissions:` block**, leaving four steps that execute repo-controlled JavaScript
  (`gen:api`, `lint`, `test`, `build`) dependent on a repository setting that lives in the
  web UI and can be flipped to read/write with no diff and no review; it now declares
  `contents: read` at the top level, which `release.yml`'s `gates` caller inherits while its
  separate `release` job keeps its own `contents: write`. And the gate did not lint
  `vite.config.ts`/`vitest.config.ts`, which `sonar.sources=.` scans as source — a
  MAIN-scope finding there would have reached the server without failing CI. Both files are
  clean under every enabled rule, so closing the gap cost nothing.

  A third round — adversarial, in an isolated worktree, 61 config mutations plus a
  differential harness against the analyzer's own predicate — found four more ways to make
  `npm run lint` pass while a pinned family regrows, all now closed and pinned. **S1082 is a
  UNION of two ESLint rules** (`mouse-events-have-key-events` *and*
  `click-events-have-key-events`, merged in one `create`); pinning half left `<li onClick>`
  free to regrow with the gate green, measured exit 0, and S6848 does not cover it because
  that rule exempts anything carrying a role. **An `eslint-disable` comment silenced the
  gate** while Sonar kept reporting the family — its channel is `NOSONAR`, not an ESLint
  comment — so `noInlineConfig: true` is now set, costing nothing (`grep -rn eslint-disable
  src` is empty). **The coverage test spot-checked five hard-coded paths**, so ignoring any
  other directory survived all tests: it now enumerates the tree. **And the globs enumerated
  what to LINT**, which left four shapes Sonar scans unlinted — a new `scripts/` directory,
  a root-level `.ts`, a root-level `*.test.ts`, a `.js` under `src/`. Both globs are now
  `**` with the narrowing expressed only as `ignores`, so a new location starts inside the
  gate rather than outside.

  The same round corrected the decorator twice more. `/^[a-z]/` is not Sonar's `Qdr`: `nlm`
  holds no SVG child elements, so `<g role="navigation">`, `<circle>`, `<text>` and every
  web component were red here and silent at Sonar — and `Logo.tsx` already contains `<g>`,
  `<path>` and `<rect>`. It now uses `aria-query`'s `dom` set plus the 20 names Sonar's set
  adds, derived by diffing the two rather than guessed; `dom` alone would have turned those
  over-fires into misses on `<svg role=...>` and `<search role=...>`. The hand-rolled prop
  readers diverged three further ways (template-literal role, role via object spread, `ROLE=`
  — `getProp` is case-insensitive), so the config now imports the very helpers Sonar calls,
  `getProp`/`getLiteralPropValue` from `jsx-ast-utils`, with both packages promoted to direct
  devDependencies since this file imports them. Eleven differential cases now match Sonar
  exactly. Two rules are knowingly still unmirrored and named in the config: S6582 and S9020
  are also `decorated`, with suppressions that would need porting.

  **That last sentence used to say "type-directed suppressions", and half of it was wrong.**
  Corrected in the follow-up, against the bundle: S9020's registration sets
  `requiresTypeChecking: false` — it is not type-directed at all. What its decorator does is
  shadow `context.settings` with three `eslint-plugin-testing-library` entries, all `"off"`
  (`utils-module`, `custom-renders`, `custom-queries`), which switches off that plugin's
  Aggressive Reporting; those are now mirrored on the config's TEST block. That change is
  pure narrowing — it can only make the gate report less — and costs 0 findings today. One
  arm remains unported and is named in the config: Sonar also drops a report whose queried
  receiver resolves to a module outside `@testing-library.`.

  S6582's description was closer but still wrong in its arithmetic: only **4 of its 6**
  suppression arms use the contextual type of the logical chain. A fifth reads
  `getTypeAtLocation` of an assignment TARGET, and a sixth uses no type context at all — a
  null-comparison predicate whose operator set is `!== != < > <= >=`, notably **without**
  `===`. That precision is load-bearing, not pedantry: on Dependabot PR #204 (package.json
  and package-lock.json only, no source) this raw rule reports 3 errors it does not report
  on `main`, all `X && X.prop === literal` inside a JSX expression container. No arm covers
  a JSX expression container and arm 6 excludes `===`, so Sonar would report them too — the
  gate is working, and the fix belongs in the source rather than in a suppression.

  ~~**Not fixed, and deliberately: the npm Dependabot lane has no release-age
  `cooldown:`**~~ — **FIXED in #203, 2026-08-30** (`be186e2` = v0.47.3). The gap was real:
  frontend updates landed the day they published, and `eslint` and `typescript-eslint` were
  both 5 days old when pinned here. `cooldown: { default-days: 7 }` now applies to all three
  lanes (uv, npm, github-actions), i.e. a MINIMUM RELEASE AGE — the one supply-chain control
  green CI cannot substitute for, since a compromised publish behaves normally and the
  payload runs at install time. Such releases are typically yanked within 24–72h.

  Three things this entry got wrong or left unstated, corrected here so the next reader does
  not re-derive them. **`interval: weekly` is not a cooldown** — it bounds how often
  Dependabot checks, not how old a version may be, so the lane was less protected than a
  reader might assume. **There was already a default of 3 days** (`default-days` defaults to
  3 when unset), so this was 3 → 7, not 0 → 7. And **security updates are exempt** by
  design, so a known-vulnerable dependency is still bumped immediately.

  On the ruling: the owner authorised this edit explicitly. Decisions #1 forbids changes
  that would make the parked frontend-deps PR go green **by narrowing what Dependabot
  proposes**; a release-age gate narrows nothing, so the park stands. Entry #1 now records
  the distinction. The 29-line header in `.github/dependabot.yml` carries the full reasoning
  in-repo.

  **Measured afterwards: editing this file CAN close and recreate already-open PRs — but
  which ones is not predictable, and two repos disagreed.** Here, within four minutes of
  #203 merging (17:17:42Z), Dependabot closed the parked #137 (17:20:26Z) and opened #204 in
  its place (17:21:27Z): *"Looks like these dependencies are updatable in another way, so
  this is no longer needed."* The lineage moved again
  (#102 → #116 → #121 → #128 → #133 → #137 → **#204**), which is why decisions #1 says never
  to quote the number.

  The mechanism is **not** the age gate rejecting anything — an edit to
  `.github/dependabot.yml` re-runs the update jobs, and a PR can be superseded when its job
  re-runs under changed config. But the blast radius is narrower than "expect the park to
  move": SpenDrop merged the identical change twenty minutes later and **its** parked PR
  survived untouched, while an ordinary dev-dep PR was superseded instead. Five of six of
  its open PRs survived. Group-vs-individual does not explain the split either. So: the
  supersede is real, it is triggered by the edit rather than the cooldown value, and
  **anything more specific than that is unsupported by two observations.** Do not infer from
  #204 carrying *more* updates than #137 that the age gate is or is not filtering; that was
  not measured.

  **The operational form, which does not depend on predicting any of it:** after any
  `dependabot.yml` change, re-derive which PRs exist, and verify the park by **content** —
  grep for the actual bump (`typescript` major) — never by number. A closed park with a
  successor is the normal lineage move; a closed park with none is the real problem, and
  only a content check tells them apart.

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

- ~~**Record the origin path at trash time so a move-back restore becomes possible.**~~
  **FIXED** on `fix/undoable-deletes` (follow-up from #189). Every mover records the origin
  in a SIBLING store — `<beets_dir>/trash-origins/<entry name>.json`, one file per Trash
  entry, keyed on the entry's own name — mirroring freedesktop.org's `info/<name>.trashinfo`
  beside `files/<name>`.

  **SUPERSEDES the sidecar** this entry first recorded (`decisions.md` 27: "a sidecar over a
  central manifest, so `Empty` needs no extra logic and a failure to record one folder cannot
  touch another's"). Owner-approved 2026-09-01. `decisions.md` 27 needs amending in the same
  breath so the next reader does not follow it back. What the sidecar got wrong: the folder
  arrives from the music library, which the threat model treats as attacker-writable, and
  `shutil.move` carries whatever it holds into Trash — so the record's own directory was
  hostile, and every guard around it existed to buy back trust that a `/data` file gives for
  free. The property the guard chain rested on ("our write wins the filename") failed twice
  in three commits. It also broke beets' source pruning: a file left inside the folder makes
  beets refuse to prune it, so the re-import a failed restore asks for left a husk behind.
  Deleted with the sidecar: the symlink refusal, the `mkstemp`/`fchmod`/`os.replace`
  choreography, the 64 KB size cap, the `PATH_MAX` origin-length cap and the
  hostile-character denylist (~230 production lines, ~220 test lines). The NUL check is the
  one member of that denylist kept — it is the only one whose consequence is a 500 rather
  than a cosmetic one.

  **What the name key costs, and where it is paid.** An entry removed OUTSIDE MusicDrop
  leaves its record, and a later folder taking that name would inherit a stale origin that
  steers a `rename()` — the same hazard inode keys were rejected for. Narrowed at the
  ALLOCATOR: `trash._unique_trash_dest` treats a recorded name as occupied, so MusicDrop does
  not hand a second folder a name whose record is still on disk, and for the names it hands
  out the residual is a burnt name (litter) rather than a wrong restore. A store it cannot
  use used to read as empty and the name went out anyway; that half is CLOSED as of this
  branch — the delete is refused instead (`trash_origins.require_usable_store` ahead of every
  mover, `origin_recorded` raising for the same fault class in the window after it), per the
  owner's ruling in `decisions.md` 28. **That is the whole of what it covers.** A folder reaching
  `trash_dir` by ANOTHER route — a hand copy, a
  restored backup, a sync client writing into the volume — asks the allocator nothing, so it
  can land on a name whose record outlived its entry and adopt it: the row offers "Exact
  restore" to a stranger's origin. Nothing detects that today; it is a stated residual (the
  module docstring in `trash_origins` states it too), not a closed hazard. Deliberately NOT
  closed by a reaper on the listing: "unlink every record with no matching entry" cannot tell
  an empty Trash dir from one whose share just dropped, and would destroy every origin in
  that state. The store IS swept, but only by `trash_manage.empty_all`, and only when that
  call removed at least one entry AND found Trash empty afterwards — a Trash it emptied
  itself, which is not the reading the listing would have to guess. So a record whose entry
  left Trash outside MusicDrop waits for the next Empty all instead of being reaped where it
  is noticed.

  **Re-derive before quoting the old framing — "the Trash rows Restore can never restore"
  was imprecise.** A Restore already existed; it re-imports through beets. The genuine gaps
  were narrower: an audio-free folder (the art/booklet husks the orphan sweep relocates)
  cannot be imported *at all*, so permanent Empty really was its only exit — that is the row
  this feature rescues — and an album that CAN be imported was re-filed by the current path
  templates rather than returned to where it came from.

  What shipped:
  * `restore_mode` on each row is `"move_back"`, `"import"` or `"refused"`, with
    `restore_note` carrying a distinct sentence for each of the FOUR ways a row loses its
    move-back (no record / files taken from a shared folder / origin no longer inside the
    library / the Trash entry is itself a symlink) and `origin` shown for whichever of them
    read a record, so the user can put it back by hand. The first three are still an
    `"import"` and stay Restore-visible and explained, never a silent re-file; the fourth is
    `"refused"` — Restore does nothing and neither does that row's own Empty, because
    at this tip (2026-09-02) `resolve_trash_child` asks `_is_symlinked_entry` about every
    component from the Trash dir down BEFORE it resolves anything, and both per-row routes
    take their child from `api/trash._child_or_404`, which runs ahead of any move or
    removal — so both answer 404 and the UI disables both controls
    (`trash_manage._restore_fields`, `models/trash.TrashRestoreMode`).
  * **No `"unavailable"` mode for `track_count == 0`**, deliberately: 0 means "nothing
    parsed as an Item", not "no music", and encoding that guess as a contract value would
    turn a UI hint into a promise. `trash_manage._audio_free_entries` warns against exactly
    this. (The rule is about a GUESS. A third value `"refused"` was added later for the one
    row whose refusal is KNOWN — a symlinked entry, which `resolve_trash_child` turns down
    on both per-row routes, on the link itself and before anything is resolved. Known at
    this tip; it was NOT known at the previous one, where a link pointing at a SIBLING
    entry resolved inside Trash and passed. See the symlink residual below.)
  * `trash_album` records `moved="items"` and never offers a move-back — its files came out
    of a possibly-shared folder, and the re-import that must follow takes a DIRECTORY
    (`ImportTaskFactory.paths` makes one album task per file when handed files), so it would
    sweep the neighbours into the album.
  * The record is written on the DESTINATION after the move: writing into the source would
    mutate a folder in the user's library and strand a file there if the move then failed.
    A failed record write can never fail a delete — it degrades the row to import-restore.

  **Residual, stated in the code, not a bug:** the origin-inside-the-library test is
  LEXICAL. An origin under a symlink that escapes the library passes it (measured). Resolving
  both sides would close that and break a legitimate symlinked-subtree layout in the same
  stroke, so the check answers "is this still my library", not "is this safe". It survives
  the move to a trusted store on CONTRACT grounds rather than threat-model ones: deleting it
  would start offering a `move_back` on rows that today carry the "not inside the current
  music library" note, which is a wire change. (The 2026-08-31 audit note this replaces —
  about a `chmod 444` plant in the MUSIC library winning against the app's failed overwrite —
  is moot: there is no plantable filename left. It is kept in the git history, not here.)

  **Second residual, unchanged in substance:** a symlinked Trash entry gets NO record.
  At this tip (2026-09-02) `resolve_trash_child` refuses a child that IS a link, or any
  path through one, on `_is_symlinked_entry` and before it resolves anything — so no route
  in the app restores such a row, and a record would make the listing offer an "Exact
  restore" whose button 404s. (The mechanism this sentence used to name — "refuses a child
  resolving outside Trash" — was the weaker predicate, and it passed a link pointing at a
  SIBLING entry, whose resolved path is inside Trash. Do not restore that wording.) The row
  reads as `restore_mode: "refused"` instead, carrying a note that says where the album's
  files really are — the SAME guard turns down this row's own Empty, so the UI disables
  both per-row controls and only `DELETE /api/trash/all` removes the link.

  **UI shipped in the same slice:** each row states its outlook before the user clicks — a
  quiet "Exact restore. Goes back to <path>" or an amber-flagged "Approximate restore."
  carrying the backend's own sentence, wired to the button via `aria-describedby`; a
  `"refused"` row keeps that layout and swaps the label for "Can’t be restored.", since a
  heading promising an approximate restore above a disabled button is the row contradicting
  itself. Restore stays ENABLED on every row EXCEPT that one (owner, 2026-08-31: *"Keep it
  enabled, warn clearly"*, amending `decisions.md` 27 — see that note for why the original
  DISABLE ruling rested on a false premise).

  **That one exception sits outside the ruling's own reason, and the owner has NOT been
  asked about it.** The amendment reasons that disabling Restore *"would have deleted a
  working recovery path in the name of safety"* — true of an `"import"` row, where the
  re-import IS the recovery path and works. A symlinked row has no such path: at this tip
  (2026-09-02) `resolve_trash_child` answers 404 to the Restore route AND to the per-row
  Empty route, on the link itself and before either route moves or removes anything
  (measured; pinned in `backend/tests/test_trash_listing_symlink_rows.py`), so the only
  reachable outcome of either live control was an error, and disabling them deletes
  nothing. That was NOT true at the previous tip, where the routes resolved first and a
  link to a sibling entry got acted on — so the carve-out rests on this tip's guard, not on
  a property the row always had. That is an argument for the carve-out, not an approval of
  it — put it to the owner and record the answer here and in `decisions.md` 27.

  The page header no longer promises "puts one back as-is", which was only ever true
  for some rows. (Corrected: an earlier draft of this note, and the backend comment it came
  from, claimed the UI disables Restore at `track_count == 0`. It does not and must not —
  `SettingsTrashPage.tsx` shows a "may still work" hint precisely because 0 means "no readable
  tags", not "no music". That 0-track hint is now re-worded rather than stacked on an exact
  row, where it would have contradicted the promise one line above it. It is suppressed on a
  `"refused"` row and nowhere else, and it keys on the REFUSAL rather than on the count.
  Every refused row MusicDrop itself creates is 0-track — it trashes an album's own FOLDER
  and `os.walk` does not descend a link — but a hand-placed link to a media FILE is listed
  by `os.walk` among `files` and `Item.from_path` follows it, so that row arrives refused
  with real tags (measured: `('linked.flac', 'refused', 1)`). Either shape gets the same
  suppression, because the hint's stated cause — unreadable tags — is the wrong one on a
  row that will not be restored at all.)

- ~~**The delete-path mount predicate accepts a root with ANY entry, so a stray file on a
  local mountpoint masks a dropped share.**~~ **FIXED** on `fix/undoable-deletes`
  (deferred 2026-08-28 by design call, from the #189 security review). `.stfolder`,
  `lost+found` or an empty leftover dir on the mountpoint made `require_library_root` pass
  while the share was gone, re-opening the ghost drop for exactly that state.
  **What shipped differs from the proposal on file in three ways — read this, not the old
  wording:**
  * **One hit, never "some/all".** The proposal said "sampling K live album dirs … some/all
    must exist". Requiring all K would lock the user out of deleting the very ghost row they
    are cleaning up: one legitimately-deleted album in the sample and the delete is refused.
    Accepting the FIRST surviving folder is the max-power/min-false-positive rule — with the
    share gone every album misses at once, so the true positive is unweakened.
  * **The sample is re-rolled (`ORDER BY RANDOM()`), not the first K rowids.** A fixed sample
    makes a false refusal *permanent*; a re-rolled one lets a healthy-but-stale library
    recover on the next attempt.
  * **Two call sites, not the whole delete path** — `trash_album_folder`'s missing-folder
    ghost branch and `_require_move_happened`'s ghost arm. Those are the only arms that drop
    rows on nothing but an absence. `trash_album`'s pre-check and `delete_artist`'s up-front
    check deliberately keep the cheap predicate (the post-condition covers them, and
    upgrading them would pay the sample per album in a fan-out).

  Direct mount checks were evaluated and **rejected on measurement, not taste**:
  `os.path.ismount()` is `False` for a healthy library on a plain local dir *and* for the
  very common "library is a subdirectory of the mount" layout, and `True` for the Docker
  bind mount (`docker-compose.yml:32`) whether or not the share behind it is alive. It
  discriminates in neither direction; `st_dev`-vs-parent is the same computation. The
  shared default was NOT made stricter — `require_library_root` is byte-identical, pinned
  by `test_require_library_root_never_samples_the_database` and a syscall-count assertion,
  because disk-sync calls it per removal and accepted the identical residual for itself.

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
  check (`app/beets/trash.py`), and `empty_all` then `shutil.rmtree`s every child
  of whatever came back (`app/beets/trash_manage.py`). Symbols, not line numbers: both
  functions have since moved by hundreds of lines. Nothing asserts the trash dir is not
  the music root, not inside it, and not the beets dir — and pointing trash at the music
  dataset (so deletes are same-filesystem renames instead of cross-device copies; the
  default sits on the small `/data` volume, which README's "Backup & restore" calls the
  only GB-scale item there) is a plausible operator move one
  typo away from `MUSICDROP_TRASH_DIR=/music`. Every other destructive path here has a
  containment check (`resolve_trash_child`, `_folder_is_shared`,
  `orphans._excluded_predicate`); the trash root has none. Small: refuse at startup when
  trash resolves inside or equal to the music dir or the beets dir.

- **The orphan sweep's ignore list does not protect an ignored dir's ANCESTORS.** (Found
  2026-09-02, same class as the entry above: a missing containment check between the
  music root and a `/data`-side directory.) Trigger: `MUSICDROP_BEETS_DIR` pointed at a path
  *inside* the music library, plus a library-scope Reorganize. Symbols, not line numbers:
  `reorganize._ignore_dirs` hands the store and the export dir to
  `orphans.find_orphan_folders`, whose `_excluded_predicate` skips those subtrees — and
  `_library_orphans` then reports the TOP-MOST audio-empty ancestor of the excluded dir.
  There is no upward climb in library scope (read at `backend/app/beets/orphans.py:130-150`,
  2026-09-02): every dir the walk recorded is judged on its own, and it is reported when it
  holds a file, holds no audio anywhere beneath it, is not named in `ART_DIR_NAMES` (`:136`),
  its parent holds no audio DIRECTLY (`:144` — the multi-disc art guard, so an album's own
  `Scans/` is spared), and its parent is either the root or holds audio somewhere beneath it
  (`:148`). "Top-most" falls out of that last condition rather than out of a walk: an
  audio-empty parent gets reported instead of its child. So the dir that gets reported need
  not be the one holding the content that made it non-empty. (Seeds mode `_seed_orphan`
  (`:168-191`) IS a climb, and this trigger — a library-scope Reorganize — takes the other
  path.)
  **Which var triggers it alone, measured on `fix/undoable-deletes`** (probe: build a music
  tree with one healthy album, then call `find_orphan_folders(music, seeds=None, ...)` — one
  call per layout, so re-deriving it costs nothing):
  * `MUSICDROP_BEETS_DIR=<music>/beets` → `['beets']`. It is the only one that fires
    unaided, and not really as an ancestor: beets plants `library.db` and `config.yaml`
    directly in that dir, so `beets_dir` has content of its own and is reported DIRECTLY —
    identically with and without the store exclusion.
  * `MUSICDROP_TRASH_ORIGINS_DIR=<music>/origins` → `[]`. Its parent is the root, and the
    root is skipped.
  * `MUSICDROP_TRASH_ORIGINS_DIR=<music>/data/origins` → `[]`. An excluded subtree is never
    recorded, so an only-child parent contributes no `has_file` and reads as empty; empty
    dirs are skipped.
  * the same placement with one file of the parent's own (`<music>/data/notes.txt`) →
    `['data']`. That is the hole: an ancestor with other content.
  * `MUSICDROP_TRASH_ORIGINS_DIR=<music>/data/sub/origins` with the file one level DOWN
    (`<music>/data/sub/notes.txt`) → `['data']`, **not** `['data/sub']` (measured
    2026-09-02 at this tip). This is the case that tells the two readings apart: `data`
    holds nothing of its own, so what is reported is the top of the audio-empty run, not
    the dir the content sits in. Whatever ends up under Trash is that whole subtree.
  * `MUSICDROP_PLAYLISTS_EXPORT_DIR=<music>/exports` → `[]`, for the reason above.

  So the two pure-container vars need at least one non-excluded file somewhere under a
  non-root ancestor before anything is reported at all — and what gets reported then is the
  top of the audio-empty run above that file, which can be several levels higher than the
  ignored dir. Blast radius depends on where Trash sits, and
  `_ignore_dirs`' docstring states both outcomes: in the DEFAULT
  layout (`trash_dir` = `<beets_dir>/trash`) the move is a directory into its own subtree,
  `shutil.move` raises, and `reorganize_jobs.runner`'s `except OSError: continue` swallows
  it — nothing is lost; with `MUSICDROP_TRASH_DIR` pointing outside `beets_dir`,
  `library.db`, `config.yaml` and every origin record land under Trash in one pass.
  **Not reachable in the shipped image**: `Dockerfile` sets `MUSICDROP_BEETS_DIR=/data/beets`
  and `docker-compose.yml` mounts music at `/music`, so the two are separate volumes; it
  needs an operator override. Not fixed on `fix/undoable-deletes`, and the docstring argues
  against the obvious fix — sparing every ancestor only moves the report one level up when
  the ignored dir is nested deeper, so it changes what the finder REPORTS rather than
  adding a guard. If it is worth closing, the cheap version is the same shape as the entry
  above: refuse at startup when `beets_dir` (or either configured store) resolves inside
  the music dir.

- ~~**A FLAT library layout defeats the delete path's presence check — it samples the music
  root against itself.**~~ (Found 2026-09-02, on `fix/undoable-deletes`, while re-reading the
  check that entry-above's sibling shipped.) — **FIXED in this branch.** Trigger: a
  `paths.default` template with no directory component — beets' own `$title` is the shortest,
  and the template is editable from the app (**Settings → Naming**, `config_editor` writes
  `paths:` straight back into `config.yaml`), so this is a supported layout and not a damaged
  install. Mechanism, by symbol: `library._sampled_library_dirs` took `os.path.dirname` of
  each sampled item path, which for a single-component path IS the library root, so
  `require_library_present` ended up asking `os.path.isdir(<music root>)` — the very
  question `require_library_root` had already answered, and the one a stray entry on a
  dropped share's mountpoint answers wrongly.
  **Measured on the shipped code before the fix** (no monkeypatching; a 200-row library built
  through beets' own `Album`/`Item`, `.stfolder` the only thing on the mountpoint): flat
  layout → `_sampled_library_dirs` returned the music root 5 times out of 5 and
  `require_library_present` ACCEPTED; the same 200 rows re-filed under
  `$albumartist/$album/$title` → `LibraryRootUnavailableError`. Blast radius: the two arms
  that drop rows on nothing but an absence — `trash.trash_album_folder`'s missing-folder
  branch and `trash._require_move_happened`'s ghost arm — so on a dropped share a flat
  library was erased one delete at a time, keeping nothing in Trash. End to end it was the
  GHOST arm that fired, not the missing-folder branch: with every album's folder equal to the
  music root, the root exists and is shared, so the front door falls through to the per-item
  mover (measured: 20 album rows became 19 with Trash empty).
  **What shipped.** `_sampled_library_dirs` is now `_sampled_library_files` and returns the
  sampled path itself; `require_library_present` stats it with `os.path.isfile`. The two
  draws are unchanged (`MIN(path)` per drawn album, one item row per drawn row on the
  fallback), and so is the cost — one `os.stat` per sample, measured identical for `isdir`,
  `isfile` and `exists`, with the same first-hit short-circuit. `isfile` rather than `exists`
  so that a directory sitting where a track should be is not read as the music coming back;
  both follow symlinks, so a symlinked library reads present while a **dangling** link reads
  absent, which is the fail-closed direction. The refusal message names files instead of
  "every album's folder", and its four pins moved with it.
  **The trade, measured rather than argued.** Keying each slot to a FILE is stricter in
  exactly one shape: an album whose folder survives with its sampled track removed by hand is
  now a miss where the folder was a hit. At 200 albums with 1 file, 5 files or a whole folder
  removed it refused 0 times in 1000 draws each; at 2 albums with one file gone, 0 of 200; at
  5 albums with four gone, 0 of 200; on the singleton fallback arm, 0 of 1000. It refuses only
  where there is no other album to draw and the missing file is the sampled one — N=1 album
  with its only (or its lowest-named) track removed while the folder stays: 20 of 20 refusals
  against 0 of 20 before. That is the shape `_PRESENCE_SAMPLE_SIZE`'s own note already
  documented as refused-by-design for a removed FOLDER at or below the sample size, extended
  to a removed file, and it is stated in `require_library_present`'s docstring and in README.
  The alternative that was NOT taken: skipping rows whose `dirname` is the library root, which
  leaves a flat library with an empty sample and the empty-sample arm accepts by design —
  i.e. it removes a flat library's presence check rather than correcting it.
  Regression: `test_library_presence_sampling.py::test_a_flat_layout_does_not_sample_the_music_root_against_itself`
  (the predicate, with the flatness of the fixture asserted from `Item.destination()`) and
  `test_trash.py::test_trash_album_folder_refuses_a_dropped_flat_share_masked_by_a_stray`
  (end to end, so the ghost arm is what is proven guarded). Both were mutation-tested by
  restoring `dirname` + `isdir`.

- ~~**An unreachable origins store makes the allocator hand out a recorded name, and the next
  folder inherits the first one's origin.**~~ — **FIXED in this branch** (PR number to be
  filled in on merge), 2026-09-02, per the owner's ruling in `decisions.md` 28 item 3: the
  delete is REFUSED while the store cannot be used. What shipped, by symbol:
  `trash_origins.require_usable_store` asks the store the three questions a delete asks it —
  `mkdir(parents=True, exist_ok=True)`, `scandir` plus a `stat` of a key that is never there,
  and `mkstemp` — and every mover (`trash_album`, `trash_album_folder`, `trash_folder`) calls
  it before it allocates, moves or drops anything. `trash_album_folder` calls it ahead of ALL
  its branches, including the two that drop rows having relocated nothing, because the
  invariant is about rows. `origin_recorded` raises `TrashOriginsStoreUnusableError` for
  EACCES/EPERM instead of answering "free", which closes the window between the check and the
  allocator's own lookup; ENAMETOOLONG stays a key-level oddity and keeps the old answer and
  its warning. `delete_album_op`/`delete_artist_op` map it to a 503 whose flat sentence names
  the store and carries no absolute path (the path goes to the log).
  **Three shapes went in beyond the one this entry measured**, each measured here: a regular
  FILE at the store path (ENOTDIR, and invisible before this — `Path.exists()` absorbs it, so
  the allocator read "no record" in silence), an absent store under a parent it cannot be
  created in, and a store that is READABLE but read-only (mode 0500 reads perfectly and then
  loses every origin silently, the same setup fault one permission bit along). An ABSENT
  store under a writable parent stays healthy and is created, which is what the first delete
  on a fresh install already did.
  **Fingerprinting was not chosen** — the alternative this entry offered — because it does not
  help the case measured below, where the two folders share a name. The original finding, kept
  for the measurement: (Found 2026-09-02, on `fix/undoable-deletes`;
  the code states it as a residual — this entry is the tracker's copy, not a second
  finding.) Trigger: an origins directory that exists but cannot be searched — a bad
  `PUID`/`PGID`, a restored backup, a stray `chmod`. Mechanism, by symbol:
  `trash_origins.origin_recorded` catches `OSError` from `Path.exists()` and answers
  `False`, and `trash_origins.read_trash_origin` returns `None` for the same fault, so the
  store's two questions AGREE on "nothing here"; `trash._unique_trash_dest` then hands out
  a name whose record is still on disk, and once the permissions are repaired that second
  folder's row reads the FIRST folder's record and offers to move it there. Measured at
  mode `0600` as a non-root user, driven through `_unique_trash_dest`: `Dummy (1)` with the
  store readable, `Dummy` under the fault. Blast radius: one wrong "Exact restore" per name
  reused while the fault lasts — the move-back writes into the music library, so the wrong
  answer is a folder landing at a stranger's path. The payload's `name` guard cannot catch
  it: the two folders share a name.
  **Answering `True` on `OSError` is still measured worse and was NOT what shipped:** it
  leaves the allocator with no exit at all, since every candidate then reads occupied
  (measured: 111,939 candidates in one second, still climbing). The refusal is a RAISE, from
  a check above the allocator and from `origin_recorded`'s own arm — never an "occupied".

- ~~**A plugin listener that raises on `album_removed` leaves the folder in Trash with its
  album row already gone.**~~ — **FIXED in this branch** (PR number to be filled in on
  merge), 2026-09-02, per the owner's ruling in `decisions.md` 28 item 4, WITH a residual
  that is stated below rather than closed. What shipped: `trash.trash_album_folder`'s
  whole-folder branch wraps `album.remove`; on a raise the folder is moved back to
  `album_root` with `fsutil.move_no_merge`, the origin record is destroyed only once the
  folder has landed, and the caller gets `TrashRowsNotRemovedError` naming the cause. A
  failed undo raises `TrashDeleteIncompleteError`, whose sentence is composed from the DISK
  (`trash._delete_whereabouts`), names both paths each in its own phrase, names BOTH
  failures, and keeps the record while anything is still at the Trash entry.
  `delete._recovery` gained an arm for each, so neither state falls to the "check the Trash
  folder" fallback any more.
  **RESIDUAL 1 — the DB half of this exact listener case is not repaired.** `Album.remove`
  deletes the album row and THEN sends the signal, and the transaction commits on the way
  out, so after the move-back the files are at the album's own folder while the album row is
  gone and its item rows remain. MusicDrop does NOT try to rebuild those rows: the user
  re-imports the folder, which is where it now is. The error says nothing about the library
  for that reason — the same exception type covers a `DBAccessError`, which raises BEFORE any
  row is written and leaves the album intact.
  **RESIDUAL 2 — the per-item mover `trash_album` gets no undo** (duplicates resolve, import
  Replace, and the shared-folder fallback of the front-door delete). A raise at its own
  `album.remove` still leaves an album the library LISTS whose item rows point inside the
  Trash container. Measured on a two-track shared-folder album: `Album.move` re-files each
  item under the container by PATH TEMPLATE, moves `album.artpath`, commits each new path as
  it goes, and prunes the source folder AND the artist folder above it — the music tree came
  back empty. An undo is therefore two `mkdir`s, N file moves from template paths to N
  recorded originals, N stored-path rewrites plus `artpath`, and a container removal, and a
  partial failure of THAT splits the album across `/music` and Trash with rows pointing at
  both. Stated in `trash_album`'s docstring; nothing in `delete._recovery` claims the undo
  for that path. The original finding, kept for the measurement: (Found 2026-09-02, on
  `fix/undoable-deletes`.) Trigger: a
  loaded beets plugin listening on `album_removed` and raising. **Measured how far away
  that is**: no plugin bundled with beets 2.13.1 listens on it (`album_removed` appears in
  the installed tree only at the emitter and in the event list), and the app's own editor
  offers a 13-name allowlist (`models/config_editor.PluginName`) containing none — so it
  takes a third-party plugin installed into the image and enabled by editing `config.yaml`
  by hand (unknown keys survive the editor's round-trip). Listed anyway because the
  consequence is the one state the delete path cannot name. Mechanism, by symbol:
  `beets.library.Album.remove` deletes the album row (`super().remove()`) and THEN calls
  `plugins.send("album_removed", ...)`, and `beets.plugins.send` wraps no handler in
  `try/except` (beets 2.13.1) — so a raising listener unwinds out of
  `trash.trash_album_folder` AFTER `shutil.move` and `_record_origin` have both run, and
  the beets transaction commits on the way out even while unwinding. Blast radius: that
  album's folder is in Trash with a valid origin record, its album row is gone and its item
  rows are still there (they are removed after the signal). Nothing in `delete.py` can see
  it — both `mutated` and `moved` count RETURNS from the primitive, so the fan-out's
  message cannot name it. **What shipped is the honest sentence, not the fix**:
  `delete._recovery`'s fallback told the user to *check* the Trash folder and what each
  answer means, instead of the old wording that said nothing had moved. That was the state
  when the owner was asked; the move-back described above is what the answer produced, and a
  failed undo does replace the original error rather than hide it.

- **The "keep Restore enabled" ruling now has a carve-out the owner has not been asked
  about.** (Raised 2026-09-02, on `fix/undoable-deletes`.) `decisions.md` 27, as
  amended by the owner on 2026-08-31 (*"Keep it enabled, warn clearly"*), reasons that
  disabling Restore *"would have deleted a working recovery path in the name of safety"*.
  That reason holds for an `"import"` row and does not reach a **symlinked** row, whose
  per-row Restore and per-row Empty are both refused by `trash_manage.resolve_trash_child`
  at this tip — so the row ships with both controls disabled, which is the shape the ruling
  otherwise forbids. The argument for the carve-out is written out in the Trash feature
  block above (the only reachable outcome of either live control was a 404, so disabling
  them deletes nothing). **That is an argument, not an approval: the owner has NOT been
  asked, and no answer is on file.** Put it to them and record the answer here and in
  `decisions.md` 27. Quote 27 accurately when you do: the phrase "every row" is nowhere in
  it (grepped 2026-09-02). Its amendment is headed *"old rows keep Restore ENABLED, with the
  warning stated"* and states the shipped shape as *"`restore_mode` is `"move_back"` or
  `"import"`, Restore stays clickable in both"* — it names no third value, so a `"refused"`
  row is outside what 27 decided rather than something it forbids. What the carve-out still
  has to clear is 27's REASON, quoted above, not its wording.

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

      # Check the RAW STRING, not urlsplit's parts. See the trap below.
      if "#" in value or "?" in value:
          raise ValueError("base_url must not contain a query string or fragment")

  **The obvious version of that validator does not work, and would ship green.** The
  canonical `parts = urlsplit(value); if parts.query or parts.fragment:` **passes the exact
  payload measured above**: a *bare trailing* `#` makes `urlsplit` return `fragment=''`,
  which is falsy. Measured 2026-08-30 —
  `http://169.254.169.254/latest/meta-data/iam/security-credentials/#` → `fragment=''` →
  passes; `http://x/y/?` → `query=''` → passes; only a *non-empty* `#x` is caught. So an
  implementer who reaches for `urlsplit` and tests with `#x` sees it blocked, ships, and
  leaves the attack untouched. The bare `#` is not an edge case here — it is precisely the
  shape that works, because the suffix has to land in an *empty* fragment for the wire path
  to end where the attacker wants.

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

- **Real authentication — auth option C, three slices, shipped 2026-08-30 (#199, #200, #201).**
  The standing long-term security item, closed in one day. Before it: 112 operations (66
  state-changing) reachable unauthenticated by anything that could open TCP to port 3030
  (compose publishes `0.0.0.0`); two unauthenticated calls ended the library (`DELETE
  /api/artists` per artist, then `DELETE /api/trash/all`); and the exposure was dual-path
  (by-IP plain HTTP *and* via Caddy), so proxy-level auth alone could never have covered the
  by-IP path the owner actively uses. The pre-existing guards were a coherent browser-CSRF +
  DNS-rebinding pair and none of them was authentication — their own docstrings said so.
  - **#199 = v0.46.0 — the backend session gate.** Every `/api/*` route plus the docs surface
    (`/docs`, `/redoc`, `/openapi.json`) behind an HMAC-signed session cookie minted by `POST
    /api/auth/login` against the scrypt hash in `MUSICDROP_PASSWORD_HASH`. Four exempt EXACT
    paths: `/api/health`, `/api/slskd/webhook` (its own fail-closed secret), `/api/auth/login`,
    `/api/auth/status`. The gate matches both the raw and root-path-stripped scope path
    (fail-closed OR), and the signing key is bound to the password hash, so rotating the hash
    evicts every session.
  - **#200 = v0.47.0 — the login UI**, which lifted the do-not-deploy hold that #199 created.
    `/login` outside the shell, a `RequireAuth` guard that remembers the destination, transport
    401 handling across both the openapi-fetch client and `apiFetch`, sign out. Plus the
    scheme-conditional `Secure` cookie (Sonar `S2092` fixed, not accepted) and `version` moved
    off the anonymous healthcheck onto a gated `GET /api/version`.
  - **#201 = v0.47.1 — the justifications re-based**, completing the option. Five comments had
    justified skipping security work by citing auth's absence. The finding that matters for
    anyone reading those comments later: they were **false when written**, not made stale —
    each asserted "only the owner can set it" while an Origin-less `curl` needed no
    credential, for ~12 weeks in the Plex case. Comment-and-docs only (AST-identical, OpenAPI
    byte-identical) plus one test pinning the anti-hardening claim.
  **Do not re-derive these from the old wording:** it was five sites, not the three originally
  scoped; `assert_public_url` must NEVER be reused on `base_url` (measured — it rejects every
  correct configuration); and the two `base_url` SSRF residuals are accepted **scoped to
  principals, not to containment**, with a named re-open trigger (a second, less-privileged
  account). Both are recorded under *Accepted residuals*. Vault: `decisions` 24, journals
  `2026-08-30-auth-session-gate`, `-auth-login-ui`, `-auth-reopen-justifications`.
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
