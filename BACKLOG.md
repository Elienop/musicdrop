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

1. ~~**The data-safety slice**~~ — **SHIPPED — PR #208, squash `6a5427c` = v0.48.0** (2026-09-02): the
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
5. ~~**First-run password setup**~~ — **SHIPPED — PR #212, squash `2d6fbc4` = v0.50.0**
   (2026-09-03; vault decisions 29). Original entry kept for its record. Why: on 2026-09-02 the owner's TrueNAS compose read `$Yy` and `$rSl`
   inside the `MUSICDROP_PASSWORD_HASH` value as variables and blanked them (compose
   interpolates `$name` in `environment:`), startup said *set but UNREADABLE*, and the fix was
   the `$$` doubling README and `docker-compose.yml` already documented — a known footgun of
   the hash-in-env pattern (Traefik basic-auth, Vaultwarden `ADMIN_TOKEN`), not operator error.
   The shape, as ruled: with NO password source the sign-in screen's pre-password notice becomes
   a **setup form** (`POST /api/auth/setup`, the fifth gate-exempt path — every census that
   counted four moves to five: `gate.py`, the OpenAPI overlay, `main.py` prose, the frontend
   `GATE_EXEMPT_PATHS`, and their pins); the hash is written to `<beets_dir>/password-hash` at
   `0600` through the same atomic writer as `session-secret`, and the caller is signed in by
   the same response; change-password is `POST /api/auth/password` behind a new **Settings →
   Account** section (current, new, confirm; a wrong current password answers **403**, not
   401, because any non-exempt 401 flips the frontend to signed-out; under the env override
   the route answers **409** even though the panel shows a notice instead of the form);
   forgotten password = delete the file and restart. `AuthStatus` gains
   `password_source: none | env | file`, and
   one resolver (`effective_password()`) replaces the four readers of `settings.password_hash`,
   the gate's included. **Precedence, ruled:** a non-empty env var wins over the file and hides
   both forms **even when its value is UNREADABLE** — refuse login, offer no setup, and say the
   compose `$$` rule in the message (the posture line's UNREADABLE arm and the `hash_password`
   CLI's stderr both gain the `$$` form). No env→file migration, no username. The residuals
   accepted with it are under *Accepted residuals*: the first-run race, the env-wins
   asymmetry, and the root-owned-file question on the `docker exec` recovery path. README,
   `docker-compose.yml` and this file change in the same PR. The 2026-09-03 review round
   (four seats plus an owner browser pass) found that the setup window is not bounded to first
   boot and that losing the data volume loses the credential — both amended into *Accepted
   residuals* below — and two fix rounds, each verified adversarially and in the browser,
   landed on the same branch before merge.
6. **Import carries no `.lrc`/`.txt` sidecars** (owner question 2026-09-14; not started, needs
   the owner's word). A yubal album imported through Add from folder left its `.lrc` files in the
   download folder: beets imports audio files only and, after a move, removes a source folder only
   when what is left is clutter (`clutter: ["Thumbs.DB", ".DS_Store"]`, beets 2.13.1), and the
   import path has no sidecar call (reorganize and tag-edit moves carry them through
   `sidecars.move_sidecars`). Recommended, not decided: on beets' per-track
   `item_moved`/`item_copied` events (both carry source and destination), carry sidecars with
   `move_sidecars` plus copy and hardlink variants (beets also sends `item_hardlinked`), tidying
   empty folders up to the import root, not the library root. Sidecars follow their track's
   operation (vault `decisions` #51), so this lands with item 7.
7. **Download providers** (owner ruling 2026-09-14, vault `decisions` #51; not started). Replaces
   the saved "Download folders" idea; Add from folder's path is a free-text field today.
   - **Operation.** ONE GLOBAL SETTING, not a per-provider mode — owner ruling 2026-09-15
     (`decisions` #53): *"instead of branching this into each provider it will be a use it or not
     setting"*. Off writes `move: yes`, on writes `hardlink: yes`, into beets' own `import:` keys;
     the app adds no per-import choice. Hardlink serves sources whose files must stay (seeding;
     downloaders that skip a track whose file exists — yubal, and deemix under its default
     `DONT_OVERWRITE`). A link that cannot be made fails the import loudly — beets raises
     `Cannot hard link across devices.` (`util/__init__.py:587-589`) — and MusicDrop does not
     downgrade it to a copy: `decisions` #57 drops the fallback #51 described. While it is on,
     no download folder empties itself, and the setting's own text must say so. NOT BUILT YET:
     there is no switch route and no switch UI — `config_editor` models `hardlink` only for its
     advisory — so today the user edits `import:` by hand in Settings -> Beets, which is the
     same keys with a worse face.
     slskd's auto-import keeps MOVING until branch 2 — the inbox routes and the drain send
     `operation="move"`, overriding the global switch by design, and `config_editor`'s
     link/hardlink/reflink advisory is where that is currently disclosed.
   - **A provider holds** a name, a kind (slskd, or a plain folder), the folder MusicDrop reads,
     the operation and, only when the source reports its own container paths (slskd today), that
     reported root (today's `downloads_prefix`).
   - **Beyond beets.** beets resolves the flags TWICE and the orders differ: `set_config` keeps
     one of move > link > hardlink > reflink, each clearing `copy` (`importer/session.py:114-138`),
     and the files stage then takes `copy` if it survived, telling `reflink: auto` apart from
     `reflink` (`importer/stages.py:278-291`). It ships `copy: yes` (2.13.1,
     `config_default.yaml`), and `hardlink` beats it, so a hardlink import needs `hardlink: yes`
     and `move: no` (beets' default).
     An explicit per-import `operation` (`move`, `copy`) now pins all five file flags plus
     `delete`, and `delete` is pinned off on every path including `default` — so a hardlink
     provider can no longer have its source removed. MusicDrop forces NO file flag for a
     hardlink and runs NO link probe (`decisions` #53, #57): a `default` import leaves the
     user's `hardlink: yes` to beets, and only turns beets' import history on for that run
     (the kept-folders bullet below). Still owed: the in-library guard (`is_in_library_source`,
     refusing copy today) covering hardlink; and a note that `write: yes` changes a hardlinked
     downloader's own file (mutagen opens it `rb+`) — acceptable for non-torrent sources, as *arr
     only documents it.
   - **Removing the source is a FIRST-CLASS feature, not `import.delete`.** MusicDrop forces
     `import.delete` off on every import path, because it is the only hard `unlink` beets performs
     on the app's behalf — `ImportTask.cleanup` calls `util.remove(old_path, False)`
     (`importer/tasks.py:332`): no Trash, no origin record, no undo, triggered by a config value
     with no UI affordance. That pin is not a refusal of the capability. If source
     removal is ever wanted, the shape follows #53 — ONE global setting, not a per-provider
     mode — and it moves the source to MusicDrop's **Trash**: visible, reversible, consistent
     with delete/replace, rather than honouring the beets key. The global hardlink/move switch
     never selects `copy` + `delete` anyway (off is `move`, on is `hardlink`), so the pin removes
     a route the design does not use.
     `copy` + `delete` is also a strictly worse move: a mid-album copy failure can leave a partial
     album filed *and* the originals gone, because `cleanup`'s "only delete what was copied" guard
     (`tasks.py:328-331`) only covers the items that made it.
   - **DEFERRED — the in-place footgun has no pre-import warning.** A config with every file
     operation off (`copy: no, move: no`) makes beets import IN PLACE, and MusicDrop's editor
     makes that two keystrokes. Measured on a default import: library rows point *into the
     download folder* (`same_inode_as_download: [True, True]`, not symlinks), so the library
     depends on files outside the music root and every feature that moves, reorganises, trashes
     or deletes an album then operates on a path outside it. `link: yes` is the same class from
     the other side (a symlink in the library whose target is the download); `hardlink: yes`
     shares the inode, so a tag write through the library rewrites the download too — which is
     what would bite a seeding user. Surfaced today only by the per-import `file operation` log
     line. It gets NO config advisory on purpose: every rule on that surface is "MusicDrop
     overrides this, the CLI still honours it", and in-place is beets' own behaviour with no
     escape hatch to name — and the rule loop validates one key at a time, so a predicate over
     five flags reads four defaults (it fired on `copy: no, hardlink: yes`, a hardlink import).
     The warning belongs in the import panel, where the user can act on it.
   - **Kept folders rely on beets' import history — BUILT 2026-09-18 on
     `feat/import-keep-downloads`.** A run whose resolved file operation is hardlink forces
     `incremental` on, with `incremental_skip_later` so a skipped album is offered again; a
     sweep forces `incremental_skip_later` off (a user's `yes` made every sweep re-bank the
     same folders). The way past the history is beets' own `-I` — `ImportOptions.incremental:
     false` (the wire admits `false` and `null`; `true` is a 422) — sent by **Import them again** and always by the
     Bank's Review now. `copy`, `link` and `reflink` configs are left to the user (`decisions`
     #53: the setting writes `hardlink`), though `link: yes` shares the same-file hazard below
     (measured: `util.samefile` follows the symlink).
     Recorded by the 2026-09-18 review seats:
     - **HIGH — CLOSED 2026-09-18 on this branch (Replace moves the old copy to Trash first).**
       Under hardlink or link, re-importing a folder whose album is still in the library and
       answering Replace left one album whose rows named files that were gone: beets skips
       `unique_path` when source and destination are the same file
       (`library/models.py:1044-1045`), so the new rows resolved to the old paths, and the
       post-run Trash pass then moved those files away. The `hardlink` and `link` params of
       `test_replacing_a_duplicate_leaves_an_album_whose_files_exist` pass unmarked now; see
       "Replace disposes of the old copy before beets places the new one" below. STILL OPEN
       from this bullet: *Keep both* on the same files leaves two albums over one set of
       files, beets' own behaviour and untested; whether the duplicate question should offer
       anything but Skip there is an owner call.
     - **A mixed run cannot name what it skipped.** beets skips a known folder before any hook
       fires, MusicDrop keeps a bare counter, and Import them again is withheld when anything
       else happened — so "1 imported · 2 already known" names no folder and offers no control.
       Future shape: known folders as read-only feed rows with a per-row "import anyway",
       bounded (a sweep skips thousands). Its own slice.
     - ~~**An attended run cannot be stopped.**~~ Import them again on a parent of N kept albums
       parks N duplicate questions and holds the single import slot; **"Stop this run"**
       (`feat/import-keep-downloads`, 2026-09-19) ends it at the question it is on. The
       N-questions half stays true; its remedy is the Stop.
     - **beets' state file fails open, silently.** `ImportState._open` swallows any read error
       at DEBUG (`importer/state.py:73-85`, a missing file included) and `_save` then
       overwrites the file, so a truncated `state.pickle` loses the whole history with no
       signal. The visible result is a duplicate question instead of a skip, not a silent
       second import. Recorded, not guarded (`decisions` #57): probing the pickle ourselves is
       a layer around beets.
     - **The bank row is deleted when Review now's start returns 202,** not when the import
       ends, and a failed delete is swallowed.
     - **Touch targets.** `Button size="sm"` is 32px and the frontend has no coarse-pointer
       floor — a primitive-level decision, not a call-site override.
   - **An import that stops part-way is noticed, not repaired — BUILT 2026-09-19 on
     `feat/import-keep-downloads`.** beets writes the rows before it places the files
     (`importer/stages.py`: `task.add` in `user_query`, placement last), so a stop during
     placement leaves rows naming the download folder — every row, when a hardlink across
     filesystems fails on the first track. `AlbumDetail.outside_library` names one such folder
     and the album page says so. It is a fact, not a cause: an `in_place` import and rows left
     in a Trash outside the music folder read the same (an edited `directory:` does NOT —
     in-library rows are stored relative and follow it; measured). beets' `PathQuery` cannot
     ask it — the library root normalises to `.` and matches nothing (2.13.1) — so the read
     reuses `_inside_library`, the mirror of beets' own `Item.try_sync` guard; string work only.
     **Two predicates since 2026-09-19, asked in order.** `_inside_library` is lexical because
     `app/beets/edit.py` must predict beets' own lexical guard byte for byte, but the album
     page asks the opposite question — *are these files really outside?* — and the security
     seat measured an album whose rows spell the root through a symlinked alias rendering the
     notice with `holds_every_track: true` and the remedy; following it re-imports and, with the
     starter config's `import.write: yes`, re-tags an album that was already filed (not
     destructive — measured: all four files stayed and Trash stayed empty). The physical
     predicate `is_in_library_source` (realpath prefix, then `samefile` up the chain) is now a
     second opinion asked per row, and only once THAT row's lexical answer says "outside", so a
     lexically-inside row still records no filesystem read, and it swallows every `OSError` → an
     unmounted or unreadable root leaves a real notice showing. Per row, not once: asking it about
     the first lexically-outside row alone hid a genuinely-stranded second row behind an
     alias-spelled first one (code seat F1, measured 2026-09-19). It moved from
     `app/beets/import_session.py` to `app/fsutil.py` and is re-exported, because `import_session`
     already imports `library` and that arrow cannot be reversed. Reachability, stated honestly:
     MusicDrop's own imports file into `lib.directory` whatever the operation — move, copy,
     hardlink, link or reflink — so only a beets CLI add or an `in_place` import through the alias
     produces such rows.
     **The app offers "add that folder again" only when every track row is a file in that one
     folder** (`holds_every_track`). That is the shape where beets asks no duplicate question —
     every row names a file the task is importing, so `find_duplicates` excludes the album and
     `remove_replaced` absorbs the rows — measured under move, copy and hardlink: one whole
     album, nothing in Trash. Everywhere else the page states the fact alone, because the
     review seats measured the remedy doing harm: on a multi-disc download the field names one
     disc, and re-adding it + Replace sweeps the other disc's download files into Trash; after
     a stop under `move` (every inbox import) the download holds only the remainder, and
     re-adding it + Replace leaves a one-track album with the placed track in Trash. In both
     the notice then cleared. Both are recoverable from Trash.
     NOT BUILT, recorded: a copy/hardlink stop AFTER some tracks landed is safe to re-add
     (measured) but rows alone cannot tell it from the `move` one, so it gets no in-app remedy;
     what finishes a `move` straddle is not established (Merge left it unchanged in one probe);
     naming a multi-disc album's common parent; a real failed `copy` leaves a partial file that
     beets steps around with a `.1` name; following the sentence on a TRASH ENTRY under `move`
     leaves a 0-track Trash row whose Restore can never succeed (no app-store refusal at
     import start on this branch); a hardlink that cannot cross filesystems stops the second
     run the same way until the operation or the mount changes.
     OPEN, OWNER'S CALL — the physical predicate silences a staging folder INSIDE the music
     root reached through a symlink. Measured 2026-09-19 (security seat L-4): `env/dlalias` ->
     `env/music/_incoming`, one album's rows spelled through the alias. Two servers, same DB:
     before the second opinion the notice named `.../env/dlalias/Ghost`, after it `null`. The row is
     physically inside and lexically outside, so beets will not relativize or move it — the
     album stays half-managed and the page says nothing. The alias shape the fix targets is the
     common one and stays fixed; this is the row where the notice's CONDITION (now physical) and
     its REMEDY (which predicts beets, whose guard is lexical `commonpath`) disagree. Two
     options, neither taken this round: (1) narrow the suppression to the alias it was written
     for — suppress only when the row's realpath differs from the row's own folder by the root
     prefix alone, keeping the measured alias fix and restoring the notice for a staging folder;
     (2) keep the physical condition and give the state its own sentence ("These files are in
     your music folder under a different path spelling; beets will not manage them until the
     spelling matches"), which is a third value on `AlbumDetail.outside_library` and therefore a
     contract change. Whichever way it goes, `backend/tests/test_album_outside_library.py` should
     carry a row spelling a folder INSIDE the root through a symlink; it has none today.
     OWNER'S CALLS: the failed run's panel does not point at the half album (its row reads "did
     not land" with no link, because the session reported no album id before it died); the
     notice has no "add this folder" control (`ImportAgainButton` already starts an import
     from a known path, and `/import` takes no `?path=`); the list pages carry no marker;
     `StatusBanner` forces `role="status"` and `items-center` (27 usages in 11 files; two
     static banners carry the live role today, three call sites work around the alignment) —
     a role opt-out plus top alignment is its own change.
   - **Residual (security seat, 2026-09-19, Low): the physical second opinion is asked once per
     lexically-outside ROW.** Measured: 12 `lstat` per row against 12 per album before the per-row
     form; 5.16 ms at 200 rows against 0.027 ms flat. Off the event loop (`run_in_threadpool`), so the
     loop is safe; the cost is a hung share, where every `lstat` can block for a mount timeout and
     the worst case is the alias-spelled album the fix targets (it never short-circuits).
     `library.py` caps its own presence sample at 5 for the same reason. Remedies, owner's pick:
     hoist the root's `realpath` out of the per-row loop (about half the per-row cost); memoize the
     answer per row FOLDER (rows of one album usually share one, so one or two chains per album; a
     file-level symlink is the shape that would differ); or cap the rows asked like the presence
     sample. Not changed on this branch. Same seat, informational: the forgiven-root record counts
     ACCEPTED starts and its sentence says "filing this import there" — an accepted start that then
     files nothing still leaves the record.
   - **An import refuses to start without the music folder, and a shown path posts back —
     BUILT 2026-09-19 on `feat/import-keep-downloads`** (PLAN §3 items 9, 10, 11, 12; each
     reproduced through the real route first). Measured before: with `directory:` missing or a
     bare mountpoint, beets refused nothing under move, copy or hardlink — it re-created the root
     on the container's own disk and a `move` emptied the download. Now `BeetsImportRunner.validate`
     asks `require_importable_library_root` (the import-side reading of `require_library_root`,
     the predicate Trash, Delete, Restore and disk sync ask unchanged) → 503 on `POST /api/import` and both inbox
     routes — except a fresh install: an EMPTY root with no item rows (`SELECT 1 FROM items LIMIT
     1`; a random one-album sample opened the gate 9 polls in 400 beside a pathless row) lets the
     first import start
     (Docker hands every new install an empty `/music` and the app never creates it; the review
     seat measured every new install refused before this arm was forgiven; Trash, Delete, Restore
     and disk sync keep the stricter predicate). **That arm's key is "the `items` table holds no
     row", which is not "this install has never imported":** a `library:` edited to a path that
     does not exist yet, a `library.db` lost or restored from before any import beside an intact
     `config.yaml`, and a repointed `BEETSDIR`/`MUSICDROP_BEETS_DIR` each reach it on a configured
     install, and the security seat measured an attended import filing four files onto a bare
     mountpoint through it (2026-09-19). Narrowed rather than closed: `require_importable_library_root`
     returns the root it forgave and `ImportJobRegistry.start` WARNs it on `uvicorn.error` once per
     ACCEPTED start, after the slot claim (a refused start records nothing), naming the root it is
     about to file into — the one record that makes a shadowed-mountpoint import diagnosable
     afterwards. The log sits there and not inside the predicate because the gate polls the same
     predicate at 2 Hz while another job holds the slot. A `?first_run` flag or a setup-screen gate would close the hole instead of
     narrowing it; that is an owner call, not taken here. The shared gate both background drains poll asks
     it too, so the slskd drain and the bank apply wait with zero row writes and zero folder walks (measured over 10 s at production
     intervals: 120 `scandir` + 120 `isdir` per minute, nothing else) and resume without a restart
     — one WARNING when the wait starts, one INFO when it ends; an OS error from the root question
     inside the gate reads as "wait" too (measured: a raise there killed the acquisition thread
     and failed the bank row; while the drains are parked the acquisition status endpoint reports
     no wait — residual. Measured 2026-09-19 with the share renamed away: `phase: running` with
     `current` set to the parked folder, not `idle`, because `_process_one` sets the phase before
     `_wait_for_gate`. A third `waiting` phase is the honest shape and is a contract change, so it
     is an owner call, not taken here). The posted path is capped at 4 096 characters
     (PATH_MAX): the placeholder resolver is quadratic and runs on the event loop — 80 KB stalled
     it 210 s. The cap bounds the string, not the time: a placeholder component that matches an
     entry, alternated with `..`, re-scanned the same directory per repeat (17 s at 20 000 entries;
     a health check queued behind it waited 16.8 s), and the inbox item `name` and Trash restore
     `folder` reach the same resolver with no bound at all (25 s / 82 s from 64 KB). Second round:
     the shared resolver refuses a `..` or `.` segment in a placeholder path (the app's own
     displayed paths carry none; a user-typed one posted back with a placeholder is refused, not
     resolved; the amplified case fell from 19.0 s to 0.1 ms), the two sibling fields are
     capped at 255 characters (NAME_MAX; an over-long name is now a 422 where it was a 404), and
     the import route resolves off the event loop because the densest 4 096-character path still
     cost ~312 ms on it. Third round, all re-measured against a real uvicorn (2026-09-19): the
     threadpool REDUCES that stall rather than removing it — the dominant cost is pure-Python
     `pathlib`/`posixpath.join` work, which holds the GIL (`os.scandir` profiled at ~0.5%), so
     2048 components cost ~331 ms of request time and 175-334 ms of loop stall over five runs
     with a 2 ms poller, against a ~0.25 ms health baseline (re-measured 2026-09-19; the earlier
     413/236 ms pair came from a 10 ms poller too coarse to see the worst gap). And the siblings
     do not "resolve one component": all of them consume their value as a RELATIVE path
     (`resolve_display_path` iterates `Path(rel).parts`), so 255 characters admit 128 components
     (`"x/" * 127 + "x"` is 255 characters and 128 components) — the cap was
     reasoned about as a NAME cap and applied to a PATH. `DELETE /api/trash?folder=` had no bound
     at all: 3600 components stalled the loop 2091 ms (2797 ms with one non-UTF-8 self-referential
     symlink planted in the Trash), so it is capped at 255 like its siblings and the bound test is
     parametrised over the query SHAPE as well as the two body ones. Letting the refusal escape `start`
     instead killed the acquisition daemon thread and burned every queued bank row (measured), and
     catching-and-reverting cost ~120 row writes/min; no backoff cap was built because there is
     nothing left to cap. A NUL in the posted path is a 422 (it 500'd on copy through the
     in-library guard's `realpath`, and started-then-failed otherwise; `os.fsencode` does not
     raise on it). A folder whose name UTF-8 cannot carry is shown with U+FFFD; posting that shown
     path back — "Import them again", the album page's `outside_library.folder` — maps it onto the
     real folder through the existing `resolve_display_path`, 409 when two folders display alike;
     a path with no placeholder reaches beets byte-for-byte as typed. Review-all with one settled
     folder vanished between listing and start was already harmless (pinned, no code).
     NOT BUILT: a nonexistent path still starts and ends `done` with 0 albums ("a typo looks like
     success") — ~22 lines because any new refusal at `start` needs an arm in the acquisition
     drain (which has no catch-all); importing a Trash ENTRY under `move` files the album and
     leaves an empty entry listed, importing the Trash ROOT sweeps every trashed album into the
     library and orphans its origin records (noisy, nothing lost); a parent of the library — see
     the `POST /import` footgun entry, now measured on a POPULATED library. The registry and
     runner reach the library through one `cast` (`require_importable_library_root`) because
     `import_jobs/` must not import beets; an adapter-exported `Protocol` with `directory: bytes`
     type-checks against a real `Library` (seat, measured with mypy) and would retire the cast and
     `validate`'s `getattr`/`noqa` — ~5 signatures + 2 registry tests that pass `object()`. Not
     this branch.
     - **`aria-disabled:opacity-50` is copied onto ~20 buttons.** The pending recipe
       (`aria-disabled`, click swallowed) has no dim of its own, so each site adds the class,
       and a pending button keeps its hover fill. Lifting both into `buttonVariants` beside
       `disabled:opacity-50` is a primitive-level decision.
   - **Replace disposes of the old copy before beets places the new one — BUILT 2026-09-18 on
     `feat/import-keep-downloads`** (`decisions` #58, corrected the same day). The duplicate
     hook moves every duplicate that has files to Trash, drops the rows of one that has none,
     and answers beets KEEP; the new album lands on the old paths (no `.1`, no `[2]`). It
     answers KEEP and NOT beets' own `remove` on purpose: `remove_duplicates` re-runs
     `find_duplicates` after the user has answered, and on an as-is import `task.add` has by
     then rewritten albumartist — measured: a compilation shown as a duplicate of
     `('A','Comp X')` had `('Various Artists','Comp X')`, an album nobody was shown, hard-deleted
     outside Trash with no error. Every row of a duplicate goes to Trash with its album,
     wherever its file lives (an in-place import, a changed `directory:` and a symlinked album
     folder all put files outside the music folder legitimately). The one exception is a row
     naming a file the run itself is reading: it is dropped and the file left alone (measured:
     after a half-finished import, re-importing the folder and answering Replace had moved the
     import's own source file out of the download folder). "The same file" is the same
     directory entry (the entry's own inode plus its holding directory's, links not followed),
     on both routes: beets' own byte comparison missed a symlink-alias spelling of the
     download folder, a bare inode would match a hardlinked library copy, and a followed link
     would match a `link`-mode library entry — both of which must still reach Trash. A bank
     apply whose collision holds an album its banked prompt did not name refuses the whole
     Replace ("The library changed since this was set aside. Decide again."), the bank row
     fails retryable, and the row's stored prompt is replaced with the collision that apply
     saw, so deciding again is a decision about what the library holds now. Filtering instead
     was measured wrong twice (the un-named album went unclassified and beets wrote through
     its dangling links; where it did dispose, a second copy stayed and the new album took
     `.1` names). If
     the music root looks unmounted, a duplicate's files cannot be read or are links to
     nowhere, no Trash is wired, the store layout is refused or a move fails, beets is answered
     SKIP, nothing is imported, and the feed row carries a short `note` saying why (a bank
     apply fails its row with the same text). Those checks run over every album in the
     collision, asked about or not, because they are about where beets is about to write. The
     banked route (`_seed_replace_from_directive`) still trashes after the run, because a bank
     apply can import nothing; it moves nothing of an old album that names the same directory
     entry (resolved path, file inode plus holding-directory inode) as an album landed in that
     run — a refiled hardlink sibling in another folder still reaches Trash. Accepted: two
     hardlinks of one file in ONE folder read as one entry, and that album's files stay in
     place untracked. What the 2026-09-18 seats left open:
     - **OWNER'S CALL — a Replace decided on a bank row with no stored prompt replaces whatever
       collides at apply time.** That is the documented recovery for "the album duplicates one
       already in your library - decide again with a duplicate action", and what a Rescan
       leaves (it clears the stored prompt). The user is shown no list. Closing it: an empty
       list refuses once and stores the live collision, as the stale refusal now does — sized
       at ~6 production lines, seven tests pin today's behaviour, and every such row would
       need two tries.
     - **OWNER'S CALL — the finished panel wears the green Success check when something
       didn't land.** `JobDone` always passes `icon={Success}`, so a run whose only album was a
       refused Replace reads "Import finished" with a green check over "0 albums imported ·
       1 didn't land" (UI/UX seat, 2026-09-18). Older than this branch: a session that died
       before `task.add` reads the same. A tone decision, not a bug fix.
     - **MEDIUM — the banked post-run route cannot refuse a placement onto broken links.** It
       runs after beets has placed the new album, so the hook's refusals do not exist there.
       Measured by the security seat: a library copy made of links to nowhere, its DB fields
       renamed so beets' byte-exact duplicate query misses it while the banked identity check
       (case-folded) still matches — the new album's audio landed outside the music library,
       `errors: []`, no note. Needs the old copy's stored paths to equal the new album's
       destination while its duplicate key differs (a `beet modify` without `-M`, a case-only
       re-tag). Not fixed with another guard: the later slice that drops the `find_duplicates`
       wrap can hand the banked albums to the duplicate hook, where the refusals already are.
     - **A dropped share with a stray entry on its mountpoint** (`.stfolder`, `lost+found`)
       passes `require_library_root`, so every duplicate reads as having no files and its rows
       are dropped while its files sit untouched on the unmounted share. Bounded to the albums
       the user asked to replace. The stronger `require_library_present` was measured to refuse
       a small library whose only album is the ghost, which is the flow ghost Replace exists
       for. A refusal at import start is planned (slice 7) and covers a share that is down
       when the run begins, not one that drops mid-run.
     - **BUG in beets' placement, reachable on `main` — an import onto a dangling symlink
       writes OUTSIDE the library.** `util.unique_path` asks `os.path.exists`, which is False
       for a link to nowhere, so beets writes to that name and the bytes land wherever the link
       points. Measured against beets 2.13.1 with a dangling destination: `copy`, `hardlink`
       and `reflink: auto` create the file at the link's target with no error; `move` replaces
       the link (safe); `link` fails loudly with "File exists". Measured end to end through a
       Replace before the refusal below existed: a `link: yes` library whose download folder
       was moved or deleted (the entries dangle), re-imported. NOT measured, expected from the
       same mechanism: Keep both, or a plain import after a disk sync dropped the rows. A
       Replace answered through the duplicate prompt (attended, or a bank apply whose collision
       beets finds) refuses in that state ("The old copy's files are broken links. Nothing was
       imported.") and deletes nothing, links included, so the user has to remove the links by
       hand; the banked post-run route does not (the MEDIUM above). Clearing a dangling link at
       a library path, or refusing placement onto one, needs its own decision.
     - **Trash on another filesystem receives a full copy of a `link`-mode entry.** beets'
       cross-device move reads through the symlink for the content and removes only the link
       (`util/__init__.py`, the `copyfileobj` fallback), so nothing is lost but Trash holds a
       copy of a file the download folder still has. Space, not safety. Code-read, not run.
     - **Two unbuilt shapes in the banked route's file identity.** A holder directory renamed
       from outside between the two stats reads a shared file as unshared (needs an external
       change during a run); a row naming a FIFO reads as present and a cross-device move would
       block on opening it. Neither was constructed.
     - **beets' error text reaches the job error unscrubbed**, absolute host path included
       (`FilesystemError: … while copying /music/…`). Accepted: the reader is the authenticated
       owner of those paths, and the path is what lets them fix the problem.
     - **A ghost's surviving cover stays in the folder, and beets overwrites it later.** When the
       old album's audio is gone but its cover is not, Replace drops the rows and leaves the
       cover untouched (`trash_album` cannot move art when no item moved, measured). The new
       album's `artpath` starts empty, and the next art save for it goes through beets'
       `Album.set_art`, which removes whatever sits at the art destination before writing
       (`library/models.py`, `util.remove(artdest)`, no `unique_path`) — so the old cover is
       replaced then, with no Trash entry. beets' own behaviour for any untracked file at that
       name, not only after a Replace.
     - **One refused Replace fails the whole bank row**, even when a sibling album of the same
       folder landed; a retry re-imports the folder and the sibling then surfaces as a
       duplicate. A bank row is a folder and has one status.
     - **Not run on the shipped Docker layout** (Trash on another filesystem than the music).
       `trash_album`'s cross-device behaviour is unchanged, but its failure now arrives before
       the import instead of after it.
   - **Delete moves an album's own files to Trash, not its folder — BUILT 2026-09-18 on
     `feat/import-keep-downloads`** (`decisions` #58; reverses #28 item 4). Tracks, the tracked
     cover and MusicDrop's lyric files go; anything else in the folder stays, and the folder
     stays only while something is left in it. Closes the released case-insensitive loss for
     audio: measured both ways on a casefold tmpfs, 2 dangling rows under the folder move, 0
     now. beets prunes folders its move empties, climbing to the music root; a keep-file holds
     it off the app's own folders (inbox, Trash, playlists) for the length of the move, so
     Delete never removes one and never creates a directory (putting a pruned folder back was
     measured to create it on a dropped share's bare mountpoint, after which the mount check
     passed). The whole-folder album mover (`trash_album_folder`, its undo,
     `TrashRowsNotRemovedError`, `TrashDeleteIncompleteError`) is REMOVED; entries further
     down that name it describe released versions. Restore still reads the `moved="folder"`
     records those versions wrote. Open:
     - **Restore brings back the audio only — owner's call.** Delete then Restore re-imports
       the tracks; the cover and the `.lrc` files stay in Trash, the origin record is consumed
       and a 0-track row remains (measured; the lyric half is pinned, the cover half is not).
       Every deleted album now, where it used to be shared-folder albums only. Item 6 above
       (import carries sidecars) would return the lyrics for every import; the cover needs
       Restore to set it from the entry.
     - **A failed row removal leaves the album listed with its files in Trash.** The error
       says to retry before emptying, a retry finishes the delete, and Empty refuses an entry
       the library still lists ("Delete the album again, then empty Trash."; a lone track gets
       "move that entry out of Trash"). The check is beets' own `path:` query (`PathQuery`),
       shared by Empty and the delete-retry side through one helper (`protected.rows_under_any`),
       asked over DIFFERENT roots — Empty's gate over every candidate spelling, the retry arm
       over the current Trash alone (only the refusing half may be generous: the retry arm
       drops rows without moving files, so widened it de-registered an album whose files sat
       in an old default Trash the page does not list; measured paired, then narrowed)
       — three rounds of hand-rolled path SQL, root spellings and inode confirms were deleted
       for it (owner, 2026-09-19: do not over-engineer what beets already has). It is asked
       with four CANDIDATE spellings the app's own settings name for the current Trash — the
       checked Trash, its resolution, the configured string as written, and the default
       `<beets_dir>/trash` — deduplicated to 1 on an ordinary default layout and 2-3 once a
       link is involved, in one `OrQuery` pass. One spelling was not enough: a row holds the
       spelling the mover used THEN, the settings resolve NOW, and `resolve_trash_dir` returns
       a configured path resolved and the default unresolved — measured through the routes
       with no hand-edited row (`mv trash bigdisk-trash && ln -s bigdisk-trash trash`;
       configuring a linked default Trash; clearing a configured one): Empty answered 200 with
       the only copy gone and the album still listed. Each now refuses, and the remedy clears
       it. RESIDUAL, recorded not guarded — beets has no identity beyond the path string: a
       Trash reachable only under some other spelling is not recognised and Empty removes the
       entry (measured in `tests/probes/alias_rows.py`: a bind mount, a second symlink, NFD
       against NFC, `STRASSE` against `Straße`). One such path needs no hand-edited row: the
       same relocate-and-symlink done TWICE (`trash -> disk1-trash`, then `mv disk1-trash
       disk2-trash && ln -sfn`) — nothing remembers `disk1`, no spelling list can name it, and
       Empty answered 200 with the album still listed (review seats, measured through the
       route; not in that probe). A hand-built
       `<trash>//Entry//01.mp3` row is matched by the root query (the retry arm treats it as
       in Trash) and not by the per-entry query (Empty removes the entry); `..` is matched by
       both; `Album.move` normpaths what it stores, so the app writes neither. beets probes
       case sensitivity per PATTERN, so the root query and an entry query can sit on
       differently flagged mounts (code-read, not built). Enumerating more spellings by hand
       is the machinery that was deleted. Cost at 100 000 relative rows, no hit, fresh
       fixture: ~24 ms with one spelling, +11 to +17 ms per further one (two independent
       runs; an earlier 98 ms figure came from a fixture directory reused across four builds).
       An item row with a NULL or empty `path` (beets stores `b''` for a pathless item;
       nothing in the app adds one) is refused by name before anything moves:
       "Nothing was moved. Fix the row in beets, then retry." The album-page notice built on
       this branch matches the failed-row-removal state when Trash is outside the music
       folder (rows naming files outside the library). The first attempt's lyric files are not recovered by the retry: they stay
       beside where the audio was, for the orphan sweep. A delete that stopped PART-WAY (some
       tracks moved) still gets a second Trash entry on retry.
     - **A `clutter:` pattern matching `.musicdrop-keep`** (`.*`, `*`) turns the protection off;
       it is logged, not prevented (a config advisory would be the place). A file the user's
       `clutter:` names is removed with the emptied folder, permanently, as in any beets move
       — measured with `['*']`: the album's other tracks count as clutter and never reach
       Trash.
     - **The keep-file's release checks identity before it unlinks**, so a file that arrives
       at the name after the plant is left alone; the stat-to-unlink window is narrowed to
       that one directory, not closed. A plant whose own `fstat` faults (EIO on a stale
       share) leaves its keep-file behind; the descriptor is still released.
     - **An artist delete on a FLAT library reads the library once per album** for the
       sidecar claim (`_stems_in_use`, the one hand-rolled path predicate left — beets has
       no "who shares this stem" query): 11 ms per album nested, 165 ms per album in a flat
       100 000-row root, so ~1.6 s for ten albums inside the swap lock. Hoisting the read to
       once per artist is the fix.
     - **Under `hardlink: yes` a Replace leaves the trashed old copy and the new library
       file on one inode** (measured through the branch's own flow). Nothing compares inodes
       there today; any future identity check on Trash must include the holding directory.
     - **A cover both case-insensitive twins track still travels with whichever is deleted**
       (characterized in `tests/probes/casefold_delete.py`).
     - **The casefold pin may skip on CI.** It needs a casefold tmpfs and an unprivileged user
       namespace; `MUSICDROP_REQUIRE_CASEFOLD` is set nowhere in `.github/`. Read the first CI
       run's skip line before calling the loss pinned anywhere but the dev box.
     - **Only Delete holds the prune off an app folder.** Duplicates resolve and import
       Replace call the same mover without the keep-file (an album imported in place into the
       inbox, then replaced) — measured. Reorganize, tag-edit moves and import run beets'
       prune over the same chains with nothing planted — read in the code, not driven. Empty
       or clutter-only directories only. One shared "hold our folders" step for every mover
       is its own slice.
   - **BUG, on `main` — a playlist can silently name a different song.** Playlist entries
     store a bare beets item id (`app/playlists/store.py`, `StoredEntry.item_id`), nothing
     prunes an id whose item is gone, and SQLite hands a freed rowid to the next insert.
     Measured 2026-09-18 with a real store and library, no Replace involved: delete the newest
     album, import a different one — the stored ids `[1, 2]` went from `Airbag 1/2` to
     unresolved to `Idioteque 1/2`, and the next `.m3u8` export names the new files. Reached
     only when the deleted items held the highest ids (the newest album). After a Replace of
     the newest album the same reuse re-points the entries at the replacement copy, which
     happens to be what the user wants there, by accident. The fix is not designed.
   - ~~**KNOWN LIMIT — a raced config Apply drops the pin and re-shadows the new config.**~~ —
     **CLOSED 2026-09-21** on `feat/import-keep-downloads` (PR #232). Every forced `import.*`
     key is an overlay on a process global, and beets' importer reads `config["import"]` live
     (`importer/session.py:91-138`, `:188-191`; no per-session config), so an Apply that swapped
     the sources mid-import dropped the pin (measured: `delete: False` became the user's `yes`,
     `duplicate_action: ask` became `remove`). Closed by mutual exclusion rather than by asserting
     the value where beets reads it: Apply and the other swap-lock holders now check the job
     slots under the claim lock AFTER taking the swap lock (`library_busy.swap_blocked_by_job`),
     so no import runs while Apply replaces the config. The artist-image reset checks only the
     artist-art slot and touches no beets config. The same gap let an overlapping copy-import pick
     up Trash restore's forced `move`; closed by the same check. Search words: overlay, pin,
     delete, Apply, swap lock, claim lock, TOCTOU.
   - **Upgrade note owed in the release.** A user running `copy: yes, delete: yes` today has
     manual imports of a plain folder silently removing the source; after the pin they keep it, so
     that folder stops self-emptying. Inbox/slskd paths are unaffected (they send
     `operation="move"`). Name `move` as the supported alternative. Also name the new boot
     refusals (owner ruling 2026-09-21): a config that started on the previous release stops at
     boot if an `include:` is missing or broken, or if the include list is over 32 entries or
     1 MiB. Before, beets loaded it without the include and said so only on stderr.
   - **Reference.** Lidarr v3.1.0 applies "Use Hardlinks instead of Copy" only on its copy path,
     as hardlink-else-copy (`TrackFileMovingService`, `DiskTransferService`), and keeps Remote
     Path Mappings per client host.
   - **yubal** (source read at tag v0.10.0). Completion is pushed only as a job `updated` event
     with status `completed` on `/api/jobs/sse`: no webhook (the README roadmap lists one), no
     auth. The Job drops the folder it wrote (`SyncResult.destination`); jobs live in memory,
     capped at 200; one playlist job spans many album folders; dedup is file existence, so a move
     re-downloads. Feasible under per-source signals (the 2026-06-08 scope cut dropped the
     settle-timer folder watcher): on a completed job, one bounded scan of the yubal folder for
     album folders changed since the job started, imported by hardlink. Upstream, copying
     `destination` onto the Job would remove the scan.
   - Not yet: a browse picker (a folder-listing route is a new ability to read the filesystem).
8. **Docs: `docker-compose.yml` and README show split `/music` + `/inbox` mounts** (2026-09-14).
   rename(2) and link(2) return EXDEV across two mount points even of the same filesystem
   (`man 2 rename`, `man 2 link`), so two bind mounts make an import move a copy-then-delete (beets'
   `util.move`, and MusicDrop's own moves) and a hardlink fail, even on one dataset. One parent
   mount fixes that only when it holds one filesystem: separate ZFS datasets behind it still copy
   (TRaSH Guides: one dataset with subfolders). Say that instead of implying the split is the
   recommended shape.

9. **Settings gets a Lidarr-style Tasks / Jobs / Logs section** (owner, 2026-09-19, parked:
   *"lets leave this for after we finish what is important here"*). Today the activity popover
   (`frontend/src/components/shell/ActivityPopover.tsx`) is the only surface for the six job
   sources composed in `frontend/src/api/useActivity.ts`, and the operator log is terminal-only.
   Shape to design, not decided: one Settings route listing running and finished jobs with their
   outcomes, plus a readable log. Depends on the done-row dismiss under Open bugs, which is the
   small half of the same complaint.

10. **Replace the six native `<select>`s with the shadcn Select** (owner, 2026-09-19, on the
    candidate page: *"the check button is blue as well"* — the native dropdown's checked mark and
    focus tint are the browser's blue, not the app's tokens; the two native checkboxes on the same
    screens were swapped for the shadcn Checkbox on `feat/import-keep-downloads`). No Select
    primitive is installed yet (`frontend/src/components/ui/` has no `select.tsx`;
    `components.json` exists, so `npx shadcn add select`). Sites: `CandidateReview.tsx`,
    `BankSection.tsx` (two), `BrowsePage.tsx`, `Pagination.tsx`, `PlexSettingsPanel.tsx`. One pass,
    all six, so the family stays one shape; the Pagination one is the only page-size control.

The 40 banked #143 Plex review Minors stay fully adjudicated (2026-08-25, every item
re-verified against v0.44.0): 12 shipped as the triage fix slice (see Recently shipped), 12
recorded below, 3 accepted as deliberate, 3 were already fixed. Of the 12 recorded, the
three that sat under Open bugs shipped in #184; the nine under Deferred minors remain open.
Dispositions with per-item evidence: the vault note `plex-143-review-minors`.

## Open bugs / hardening

- **Activity popover: a finished job's "Done" row cannot be dismissed and stays until the server
  forgets the outcome.** Owner, 2026-09-19, from the TrueNAS instance: three Done rows (lyrics,
  artist art, Reorganize) with *"no way to clear them"*. Cause: each job registry keeps its last
  outcome until that job kind runs again or the process restarts, and the popover shows whatever the
  server reports; `useActivity()` drops a dismissed row only when its state is `failed`
  (`frontend/src/api/useActivity.ts`, the dismissed filter) and `ActivityPopover.tsx` draws the ✕
  for failed rows only. Fix shape (frontend only): draw the ✕ on done rows and let the filter drop a
  dismissed done row; safe because every run mints its own uuid id, so a dismissal cannot hide the
  next run. Open choice: dismissals live in `sessionStorage`, so a dismissed row returns in a new
  tab; `localStorage` would make a done row's dismissal stick, and would also make failed rows'
  dismissals per-browser instead of per-tab. On `main`; not this branch's change.
- ~~**Test-order flake: a SUBSET run of `backend/tests/test_import_start_guards.py` fails with
  `confuse.exceptions.NotFoundError: timeout not found`**~~ — **CLOSED 2026-09-21** on
  `feat/import-keep-downloads` (PR #232). **The cause recorded here was wrong in four places**, all
  corrected by a diagnosis agent's measurements and re-checked against the code:
  - **Not pre-existing against `main`.** The file, the two leaking tests and both victims were all new
    on this branch (2026-09-19, PR #232); "pre-existing" was only true of later rounds on the branch.
  - **The worker thread never raises.** Two tests started a real import and returned without draining
    it, against the file's own rule (`_drive`, see the comment at `test_import_start_guards.py:174`).
    The leftover worker RELOADS beets' config just after the between-test reset, and confuse's
    `LazyConfig.read()` sets `_materialized = True` BEFORE it loads the sources
    (`confuse/core.py:724`), so for ~5 ms the config looks loaded but holds no defaults. The NEXT test
    to read `timeout` (`beets/library/library.py:83`, via `build_library`) raised — an innocent
    neighbour, never the test that leaked.
  - **In the test body, not SETUP** (pytest reports `when=call`), and **not the `clear()` hazard
    conftest describes** — it is `read()` setting the flag before loading while another thread lives.
  - **Not a design question.** Two drains closed it.
  Why the full suite hid it: by then each test's `tmp_path` setup scans a temp dir of ~7,500 entries
  and runs a few ms slower, so the neighbour usually arrived after the window closed. Measured: the
  two-file subset failed **3 of 12** plain and **5 of 5** with the window held open; after the fix
  **0 of 12** plain and **0 of 5** held open, and removing either drain alone leaves that test's
  import alive at the reset (the second also fails its neighbour again). Search words: flake,
  subset, order-dependent, `timeout not found`, LazyConfig, `_materialized`, `_drive`, drain.
- **`POST /api/trash/restore` and `DELETE /api/trash` still resolve caller-named relative paths
  on the event loop.** Same class as the import start, which was moved off it on 2026-09-20 after
  the security seat measured a **10002.7 ms** loop gap against a 2.1 ms idle baseline on a hung
  mount (FUSE stand-in with a sleeping `getattr`; the same probe on the previous commit read
  2.4 ms). These two routes take the same 255-character / 128-component input shape and were not in
  that round's scope. A hung share therefore still freezes every other route, live updates and
  `/api/health` — and the image's `HEALTHCHECK --timeout=5s --retries=3` (`Dockerfile:44`) marks the
  container unhealthy after ~15 s, which a supervisor (autoheal, a k8s liveness probe) acts on by
  restarting mid-operation. Fix shape: `run_in_threadpool(partial(...))` around the resolve block,
  exactly as `app/api/import_.py` and `app/api/acquisition.py` now do; the house test oracle is
  `asyncio.get_running_loop()` inside the probed callee asserting `on_loop == [False]`, NOT a timing
  assertion (a timing oracle is vacuous here — under the mutant the blocking call owns the loop, so
  the `await` that starts the timer cannot resume until the stall ends and the measurement reads
  clean). Search words: event loop, blocking stat, hung mount, NFS, healthcheck, threadpool.
- **Forty concurrent import starts exhaust the process-wide thread pool, and `/api/health` keeps
  answering 200 while they do.** Measured 2026-09-20 on a real uvicorn against a hung FUSE mount.
  `ImportJobRegistry.start` calls `runner.validate` BEFORE `claim_slot` (`registry.py:298` vs
  `:314-315`), so the single-job slot does not bound how many source stats are in flight. anyio's
  default thread limiter is **40 and process-wide** (anyio 4.13.0), and FastAPI draws from it for
  every sync `Depends` callable — all 18 of this app's are plain `def` — and for the scrypt derive
  at `app/api/auth.py:459`. With 40 stuck: `/api/health` 200 in 0.65 ms, `/api/imports/active`
  timed out at 20 s, `POST /api/auth/login` timed out at 20 s. **The single-request case got
  strictly better** in the same round (on `main` ONE request took `/api/health` to 18.0 s against a
  0.0005 s baseline; now one request costs one token and everything else stays sub-3 ms), so this
  is a saturation-only regression — but a specific one: a dead app now reads healthy, where before
  the loop stall took the healthcheck down with it. The shipped `docker-compose.yml:44` sets only
  `restart: unless-stopped`, which acts on exit rather than on `unhealthy`, so nothing auto-restarts
  either way today — the cost is to operator monitoring and to any autoheal/k8s liveness probe.
  There is no `--limit-concurrency` (`Dockerfile:61`) and no inbound rate limiting anywhere
  (`TokenBucketLimiter` is the OUTBOUND artwork fetcher). Caps SHIPPED 2026-09-20: a 1-token limiter on the
  import-start path (bounded at 30 s, then 503) and a 4-token limiter on the inbox filesystem
  reads, which the slskd webhook's three hops now also take. This entry stays for the two halves a
  cap does not fix: the healthcheck still answers 200 on the loop while the app cannot serve a
  route that touches a disk, and **the inbox reads' WAIT is unbounded** — a hung mount parks
  `GET /acquisition/status`, `GET /acquisition/inbox/items` and `POST /acquisition/review-inbox`
  at the limiter with no answer and no sentence. Two seats rated that High; it is recorded rather
  than fixed because the same callers hung inside `os.walk` before the cap existed (so it is not
  a regression) and bounding it means a 503 on two polled GETs — a contract change plus new UI
  handling. Fix shape if taken: `anyio.move_on_after(~5s)` around the acquire, mirroring
  `import_start_admission`, with a short sentence naming the share.
  Search words: anyio, thread limiter, 40 tokens, saturation, healthcheck lies, liveness.
- **`POST /api/acquisition/review-inbox` reports an UNREADABLE inbox as "nothing to review".**
  `app/acquisition/inbox.py:219-222` (`settled_folders`) and `:154-157` (`count_pending`) both
  return `[]`/`0` on any `OSError`, so with the inbox share unreadable the route answers
  `200 {"started": false, "pending": 0, "in_flight": 0}` — "your inbox is empty" — for a share the
  app cannot read. Measured identically on `main` and this branch, so pre-existing, and the safe
  direction by design (skipping beats sweeping a folder mid-write). But it is the one route where
  the 2026-09-20 permissions diagnosis does not reach the user: the same misconfiguration says
  "can't be read" on the two start routes and "all clear" here. Fix shape: let `settled_folders`
  distinguish empty from unreadable (return `None`, or raise) and have the route answer 422 with
  the same `strerror` sentence — a return-contract change with several callers, which is why it is
  recorded rather than folded into that round. Search words: inbox, unreadable, empty, settled,
  OSError swallowed.
- **`xs` and `icon-xs` buttons render an unsized glyph at 12px, off the design system's icon
  scale.** `frontend/src/components/ui/button.tsx:29` gives those sizes
  `[&_svg:not([class*='size-'])]:size-3`, and twMerge keeps exactly one of the two same-prefix
  tokens, so the base 16px is replaced rather than joined. At 12px Phosphor's light stroke (12 of
  256 units) is **0.563px** — sub-pixel on a 1x display, a grey hairline rather than a line. The
  spec's steps are inline 16 / banner 20 / hero 40. Two live sites were moved to 16px on
  2026-09-20 by the owner's call (the Activity popover's dismiss glyph and the job row's View
  caret, both by an explicit `size-4` on the glyph so the buttons keep their own box). **The sweep has an EMPTY population** — measured
  2026-09-20 (UI seat): those two WERE the only `xs`/`icon-xs` buttons in the app (`size="xs"` →
  `JobProgress.tsx`, `size="icon-xs"` → `ActivityPopover.tsx`), and both now opt out with an
  explicit `size-4`. So the `size-3` rule currently governs nothing and is a trap armed for the
  next `xs` button rather than a live defect. Note `xs`/`icon-xs`/`icon-sm`/`icon-lg`/`icon-xl`
  are this project's additions, not shadcn's canonical four, so changing that token is fixing a
  local default and not overriding a primitive.
  Search words: icon scale, xs, icon-xs, hairline, sub-pixel, twMerge, size-3.
- **The "Review all" refusal no longer NAMES a folder — do not rebuild it.** Deleted 2026-09-20
  (~90 app lines, 4 tests), because it named the WRONG folder in every scenario it can reach.
  Measured three ways: `os.stat` on a folder SUCCEEDS at `0o000`, `0o444` and `0o111`, so a
  folder's own permissions can never raise the `EACCES` this arm needed; the only shape that does
  is the INBOX PREFIX losing `+x`, and then every child refuses identically; end to end,
  `missing_source_error` stops at the first non-absent errno, so the sentence read
  `“Artist - Album A” can’t be read. Permission denied.` for a healthy folder. The batch 422 now
  uses the shared singular. The forged-clause sanitiser (`_nameable`, `_QUOTES`, `_NAME_CAP`) went
  with it — that hole existed only because a peer-chosen string was interpolated into operator
  prose. Unsettled: ESTALE on a child that is itself a mountpoint might single one child out; not
  reproducible without NFS. Search words: Review all, names the folder, _nameable, forged clause.
- **A dropped unreadable folder is named only in the log.** `status.error` carries the shared
  path-free sentence, deliberately: interpolating the basename there would open a second forge
  surface for one line of text. But `has_audio` returns False for that folder, so it appears in no
  listing — the operator has only the WARNING line (`%r`) to identify which folder. Reversible if
  the sanitized basename is wanted there too. Search words: drop, status.error, which folder.
- **The queue's GLOBAL defer arm is deliberately unbounded.** The unreadable arm is terminal (a
  drop, not a retry), but the `RuntimeError` / `LibraryRootUnavailableError` arm defers, because
  its condition is global rather than per-folder: bounding it would drop every queued download
  during a long NAS outage. Deliberate, not an oversight. Search words: defer, unbounded, share outage, NAS.
- **The Review page's refusal has no automatic expiry.** A folder whose permissions are fixed on
  the server reappears in the listing with the stale red sentence above it until the operator
  presses Dismiss. Auto-clearing on any refetch was rejected because the refetch that drops the
  folder is exactly the one the sentence must survive; the only honest predicate found was "a later
  listing gained a row it did not have while the refusal stood", which over-clears when an
  unrelated download lands. Search words: refusal, expiry, dismiss, stale sentence.
- **`logger.exception`'s traceback re-opens log injection for beets' own errors.**
  `app/bank/apply_runner.py` — all three `item.folder` sites now use `%r`, and `str(OSError)`
  renders `filename` with `%r` so a newline is escaped there. But `str(beets.util.FilesystemError)`
  interpolates the path RAW, so a folder named `Album\n<forged record>` produces a complete forged
  line inside the traceback, bypassing the `%r` on the format argument. Reachability through
  `_apply_one` is thin (DB reads and JSON writes), so this is a residual rather than a demonstrated
  hole. Search words: log injection, traceback, logger.exception, FilesystemError, beets.
  **Sharper since 2026-09-21** (security seat, PR pending): `_row_error`'s OSError arm now tells
  the operator to "check the server log", so this stream is the remedy the app points at rather
  than a residual nobody reads. And now that app records carry uvicorn's `LEVEL:` prefix, a forged
  line reading `WARNING:  ...` sits among genuine ones. Still Low under this threat model - the
  actor is an unauthenticated remote peer gaining a deception / anti-forensics primitive in
  `docker logs`, not code execution or data access.
- ~~**A pre-existing flaky test, with a control.** `beets.config["timeout"]` raises
  `confuse.NotFoundError` inside `build_library`~~ — **CLOSED 2026-09-21** (PR #232). The same flake
  as the "Test-order flake" entry above, recorded twice; its cause, the correction of what was
  recorded here ("pre-existing", "during test SETUP", "resetting confuse deterministically is a design
  question") and the measurements are all in that entry. Search words: confuse, LazyConfig,
  NotFoundError, timeout, flaky, build_library.
- **Backend user-facing copy is half-curly: sweep the rest.** Owner's call 2026-09-20 — app copy
  uses TYPOGRAPHIC punctuation, because the user never sees a `.py` file, they see one page, and
  the frontend already uses `’` (153 sites) and quotes user data with `“ ”`
  (`Results for “jazz”`). That round converted the two sentences it added
  (`That folder can’t be read.`, `That folder doesn’t exist.`) and their pins; its third, the
  batch `“<name>” can’t be read.`, has since been deleted with the naming feature.
  **Not swept**: the other user-facing backend sentences still use straight apostrophes, so the same alert can show both dialects —
  e.g. `Couldn't reach the slskd server.` (`app/slskd/service.py`), `Couldn't reach the Plex
  server.` (`app/plex/service.py`), `Couldn't read the library files.` /
  `Couldn't save this playlist.` (`app/api/playlists.py`), `Rescan isn't available for this
  album.` (`app/beets/import_session.py`). Measured: an AST pass over `backend/app` finds 175
  string literals containing a straight apostrophe, but the large majority are DOCSTRINGS, which
  are not in scope — the sweep is only the sentences that reach a response body or a rendered
  field, on the order of 30-40. Each has test pins, so it is mechanical but not trivial.
  **The lint blocker is already cleared**: ruff's `RUF001/2/3` flag `’` as confusable with
  `'` (20 errors on this round's three sentences alone), so `backend/pyproject.toml` now sets
  `allowed-confusables = ["\u2019", "\u201c", "\u201d"]` — the rule stays live for what it is
  for (a Cyrillic `а` or Greek `ο` in an identifier still fails), so the sweep needs no
  further config. Search words: apostrophe, curly, typographic, U+2019, copy dialect,
  straight quote, RUF001, allowed-confusables.
- **The acquisition queue's dedupe key is recomputed through `resolve()` twice, so a symlink that
  disappears leaks a `_dedupe` entry and the queued count never returns to zero.** Measured
  2026-09-20 (security seat, while auditing the drain): `enqueue` and `_process_one` each compute
  `str(folder.resolve())` independently (`backend/app/acquisition/queue.py`). With a symlinked
  parent alive at enqueue and gone by process time the two keys differ
  (`.../real/album` vs `.../link/album`; control: a plain directory gives matching keys), `_finish`
  then discards a key that is not in `_dedupe`, the set leaks the entry, `status().queued` is
  permanently off by one and `_phase` never returns to `"idle"` (`queue.py:271`). Pre-existing —
  both keys were already recomputed before the source-missing round — but that round's new terminal
  arm is a third way to reach it. Fix shape: resolve once at enqueue and carry the key with the
  item, rather than re-deriving it from a path whose resolution can change. Search words: dedupe,
  resolve, symlink, queued count, phase never idle.
- **Timing flake family — a registry test polls for the album ROW, then pushes a reply before the
  worker has PARKED its slot.** Seen in three full `make coverage` runs on 2026-09-19 while two review
  seats ran suites on the same box: `test_import_duplicate_api.py::test_record_duplicate_decision_unblocks_and_marks`
  (`KeyError: no duplicate parked at index 0`, `ImportBridge.push_duplicate_decision`) and
  `test_import_registry.py::test_record_choice_duplicate_raises_runtimeerror` (`KeyError: no album
  parked at index 0`, `ImportBridge.push_choice`). Both tests are unchanged since `main`; the same
  file's `_poll_dup_prompt` docstring names the race (outcomes drain first, parked rows second). 0 of
  10 module runs failed under coverage on either tree when run alone. Fix shape: poll the parked
  prompt (or the bridge's slot), not the row, in every registry test that pushes a reply. Not fixed.
  **Checked 2026-09-21: a DIFFERENT cause from the `timeout not found` flake, and genuinely
  pre-existing** — these tests use `FakeImportRunner`, which never reads beets config, and fail with
  `KeyError: no … parked at index 0`; both files and both named test bodies are byte-identical to
  `origin/main`. `fakes.py` and `registry.py` did change on this branch, so its rate here versus
  `main` is unmeasured.
- ~~**Acquisition drain threads outlive their tests**~~ — **CLOSED 2026-09-21** on
  `feat/import-keep-downloads` (PR #232). This entry called the leak harmless; it was not. With
  `tests/test_store_layout_boot.py` run before `tests/test_import_start_guards.py`, 4 tests failed
  (code-review seat, re-run by hand). The cause was the lifespan: `acquisition_queue.start()` ran
  before the bank-reconcile refusal, which re-raises without stopping it. The start now follows
  that refusal. Thread census over the full suite: no drain thread alive after any test (on the
  parent commit, 1, left by `test_an_unreadable_import_bank_gets_the_one_error_line_too`). Search
  words: thread leak, acquisition, drain, census.
- **Save accepts a config that stops MusicDrop starting** (security seat, 2026-09-21, measured;
  owner: record it, fix on the next branch). Three ways, each measured: Validate is clean and
  Save writes the file, then the next start refuses it.
  - A value beets rejects only when it reads it typed: `musicbrainz: no`, `plugins: 5`,
    `directory: 5` (`ConfigTypeError`).
  - A skipped include, which Validate and Save only advise on; Apply and boot refuse it.
  - YAML that ruamel accepts and PyYAML refuses: `!!python/object/apply:…` tags, complex keys
    such as `? [a]`, and a bare `=` or `<<`. (A NEL/LS/PS character in a value, or `{k: 0:}`,
    is read too, but a Save rewrites it into YAML beets loads.)
  - Anchor names outside `[A-Za-z0-9_-]`: `&x.y`, `&é`, `&a&b` save with a 200, and beets'
    scanner then refuses the file. A `:` ends the anchor name for beets: it reads `k: &a:b 1` as
    `':b 1'`, refuses it only once an alias uses it (in its parser), and sees `&t:x` beside
    `&t:y` as a reused anchor where the editor does not (review seats, 2026-09-23, measured;
    predates the branch).
  - A ruamel warning prints the whole value of an explicit `!!float` tag that holds an `e` and
    no dot (`!!float Zq7Secrt`) to stderr (`ruamel/yaml/constructor.py:1148`, the round-trip
    constructor). The value is
    then refused. Only the operator can write that tag (security seat, measured; predates the
    branch).
  - An `!!omap` sequence counts as a mapping to the editor at any depth (`import: !!omap
    [copy: yes]`, or the top level): Validate is clean, Save writes it back (`[copy: yes]` as
    `[copy: true]`), and beets refuses the file ("expected a mapping node, but found
    sequence"); Apply refuses before unloading anything. The reverse too: beets accepts
    `!!omap {…}` (`confuse/yaml_util.py:83`) and the editor refuses it (review seats,
    2026-09-23, measured; predates the branch).
  - Validate rows that name internals: a non-string `directory:` or `library:` (including one
    typed with no value yet) reads "Input is not a valid path for <class 'pathlib.Path'>", and
    a bad `import.reflink` gives two rows, `import.reflink.bool` and
    `import.reflink.literal['auto']`, both with no line (review seats, 2026-09-23, measured).
  - **The planned fix, owner ruling 2026-09-23 ("Cut and ship", after asking whether this was
    over-engineered):** Validate and Save check the text with beets' own loader and typed reads
    (`confuse.YamlSource` with `beets.config.loader`, which Apply already reads config.yaml
    through: `read_config_document`, `app/beets/setup.py:111`), and keep ruamel only for
    writing, because it keeps comments. That closes this whole list at once and deletes the
    editor's `_RefusingComposer` (`app/beets/config_editor.py`), which exists only to make
    ruamel refuse a reused anchor as PyYAML does. Do NOT keep patching ruamel or rewording
    Pydantic messages one case at a time.
  - Not a start refusal, but the same typed-read gap: `write`, `copy` or `move` set to a number
    (`write: 1`) passes Validate and Save, and beets' `.get(bool)` refuses it. An import refuses
    it before adding any row (the pre-check), and an album edit answers a bare 500 (review seats,
    2026-09-23, measured).
  Apply is no longer part of the harm: when beets rejects a value after the teardown, Apply puts
  the running config back and answers 422 with beets' error (owner ruling 2026-09-23). It
  answers 500 only if that restore fails too. Validate and Save DO refuse an include list over
  Apply's caps (32 entries or 1 MiB; code-review seat, measured). The Naming save answers 200 on
  a file of the third kind and writes it back with only its two keys changed (security seat).
  The other direction is safe but blocks editing: ruamel refuses a duplicate key that beets
  loads (PyYAML keeps the last), and a bare value starting with `%`, such as
  `default: %the{$albumartist}/…` as beets' own docs write it (confuse allows it,
  `confuse/yaml_util.py:70-73`). So Validate, Save and the Naming routes refuse a file Apply and
  boot accept (review seats, measured). A negative leading-zero int (`-0644`) is the reverse: a
  ruamel bug writes it back as `!!int '0-644'`, which beets cannot load, so Apply answers 422
  and a restart refuses. The editor's resolver reads a copy of beets' own loader table (PR #232
  rounds 10-11): over 101,360 plain values, each resolves to the type beets gives it except the
  negative leading-zero int. Still different: tagged values (`!!str x` reads as a ruamel
  TaggedScalar, `!!bool 'y'` as True where beets errors), a timestamp with more than 6 fraction
  digits (ruamel rounds, beets truncates), and the YAML listed above (review seats, measured).
  Fix shape not designed: Validate would parse with beets' own loader (as
  `setup.read_config_document` does) and ask beets' typed reads, not only ruamel. Search words:
  L4, ConfigTypeError, musicbrainz, boot,
  validate, save, include, typed read, ruamel, PyYAML, duplicate key, y/n, implicit resolver.
- **Small residuals of Apply's restore and the include gate** (review seats, 2026-09-23).
  - Each restore installs the plugins' default sources again: 5 more per restore with two
    plugins. The values are unchanged. The list resets on the next good Apply or restart.
  - A refused config's `pluginpath` stays importable after the restore, ahead of the bundled
    plugins: beets adds it (`beets/plugins.py:381,385`) before the check that fails. Setting
    `pluginpath` already runs the operator's code, so this adds no power.
  - ~~The Naming panel shows "Could not load naming config." for its 422s.~~ **CLOSED
    2026-09-23** on `feat/import-keep-downloads` (PR #232): the Naming panel's load and Save and
    Settings → Beets' Save now print the server's `config_on_disk` sentence. It showed the fixed
    text for a `config.yaml` that cannot be read, does not parse, or is not a mapping, because
    the frontend never read that body.
  - ruamel's round-trip drops a comment that sits before `---` and any `%YAML` line, and
    indents a comment that follows `--- ` on the same line, on Save and the Naming save alike.
    These comment changes alter no value beets reads. A NEL character (U+0085) in a submitted
    string comes back as a space.
  - ~~Validate and Save accept a reused YAML anchor.~~ **CLOSED 2026-09-23** on
    `feat/import-keep-downloads` (PR #232). ruamel only warned (`ruamel/yaml/composer.py:130-137`),
    and the warning printed both file lines to stderr, secrets included. Save then wrote a file
    boot refuses. The editor's composer now refuses it, as PyYAML does (`yaml/composer.py:74-77`),
    wherever both read the anchor names alike (see the anchor-name item above).
  - A `config.yaml` that is not UTF-8, cannot be read, or is not a regular file opens as an
    empty editor and does not say why. Its only lint rows are the two "Field required" rows
    (`directory`, `library`) on line 1. For a missing, unreadable or non-regular file the banner
    says "config.yaml is saved but not loaded yet", which is false (code seat, measured for
    absent, EACCES, a FIFO and a directory). Since Edit works while Apply is pending, such a
    page also invites a draft that every Save refuses with a 422 naming the cause, for example
    "config.yaml could not be read: No such file or directory." (UI seat, measured). beets loads
    a UTF-16 file with a BOM, so such a file can be running. Both Saves refuse a non-UTF-8 file
    with 422 "config.yaml is not UTF-8.", whatever sha they are sent, and write nothing. On
    `main` a Save from the empty editor replaced the file (measured: a Plex token lost; fixed in
    PR #232). Re-save the file as UTF-8 to edit it here. Search words: UTF-16, BOM, empty editor,
    sha256, not UTF-8, 422.
  - Save, the Naming routes and `GET /api/config` open `config.yaml` only when it is a regular
    file. A FIFO swapped in between that check and the open still blocks, the same gap confuse's
    own `os.path.isfile` read and Apply's gate have. The check bounds the file's type, not its
    size: our routes read a huge or sparse regular `config.yaml` whole. beets reads a huge VALID
    file whole at boot too, but PyYAML reads in 4 KiB chunks and stops at the first NUL
    (`yaml/reader.py:146-178`), so for a sparse file of NULs our routes would exhaust memory
    where boot refuses at once (security seat, measured with a finite 256 KiB file). Only
    someone with write access to the beets dir can plant one; no cap was added.
  - Validate's message for a non-list `include:` or a non-string entry names ruamel's internal
    types (`ScalarFloat`, `CommentedMap`) where Apply's says `float`, `OrderedDict`.
  - At boot, the starter config is written through a dangling `config.yaml` link, to wherever it
    points (`setup.py` ~196-203). Only someone with write access to the beets dir can plant one.
  - A Save resolves a symlinked `config.yaml` twice: to read it, and again (`realpath`) to write
    it (owner ruling 2026-09-23: write through the link). A link re-pointed between the two sends
    the bytes to the new target. Another file there keeps its mode, and its own text is lost
    with no 409. A dangling target is created with the umask default, missing folders included.
    A loop replaces the link where it closes, possibly in another folder, with a regular file at
    the umask default. Only someone who already controls `config.yaml`'s content can re-point
    it, and that content already runs commands (beets' hook plugin on `library_opened`).
    `realpath(strict=True)` would refuse the dangling and loop cases (review seats, measured).
  - The folder fsync runs after the publish, so an EIO there answers "config.yaml could not be
    written" after the new bytes landed. A retry on the same base then answers 409 (measured
    with an injected EIO).
  - A Save publishes a new file, so only the mode carries over: the owner becomes the app's, the
    group the app's (or the folder's, in a setgid folder), and a per-file ACL entry, extended
    attributes and another hard link to the old file do not follow. For a regular
    `config.yaml` this predates the branch (security seat, measured). A Save killed mid-write
    leaves its temp, new text included, in the target's folder until a later Save there sweeps
    it after an hour.
  - The artwork toggle reads its `_enabled.json` at startup with no regular-file check
    (`artwork/toggle.py:25`, from `main.py:404`): a FIFO there blocks startup (security seat,
    measured; predates this branch). Only someone with write access to the data dir can plant one.
  - The bank, slskd, Plex and playlist stores and the password-hash file re-read
    `MUSICDROP_BEETS_DIR` on every call, so they follow a beets-dir symlink re-pointed while
    MusicDrop runs. Apply uses the dir resolved at boot. Measured: after a re-point to a dir with
    no hash file, the password reads as not set and first-run setup opens. Deleting the file
    reaches the same state, so this adds no power. Replacing the booted dir itself with a link
    (`mv beetsA beetsA.old; ln -s beetsB beetsA`) also moves Apply to `beetsB`: a good Apply
    loads it, and a failed one restores into it while answering that nothing was changed.
  - The boot refusal line names only the exception class for a tagged value in an include
    (`!!bool`, `!!int`), but the traceback uvicorn prints for the failed startup still carries
    the cause, and with it the value. beets prints the same line.
  - The GET artist-image route's refill closure keeps the library handle from its dependency.
    A lookup that races an Apply can read the old library. It is read-only.
  - A FIFO swapped in between the boot gate's open of an include and beets' own open still hangs
    boot. It needs write access to the beets dir.
  Search words: restore, sources, pluginpath, naming 422, get_mbid, FIFO, include race, `---`,
  re-pointed symlink, password hash, first-run, traceback, __cause__.
- **`_CONFIG_FORCE_LOCK` may no longer be reachable under contention** (code-review seat,
  2026-09-21, reasoned; not measured). Its two callers are the import worker, which runs only
  while its slot is claimed, and the Trash restore, which now refuses while any import holds a
  slot. So the Restore arm for `ImportConfigBusyError` was deleted as unreachable (a timeout now
  surfaces as Restore's 500). If a measurement confirms that no two callers can overlap, delete
  the lock and its 5 s timeout (`app/beets/import_session.py`) rather than keep a guard nothing
  reaches. Search words: force lock, ImportConfigBusyError, restore, overlay, reachability.
- **Settings → Beets and Naming: small residuals left by PR #232** (review seats and
  implementers, rounds 15–18, 2026-09-23; recorded, not built, under the owner's "Cut and ship").
  - The Save button stays on while the conflict panel is open, and a click does nothing (Ctrl+S
    already checks for the panel). Predates the branch.
  - Text typed during a Save's round trip is lost when the re-read arrives. Predates the branch;
    a fix needs a design call.
  - After an Apply 409 whose job has already ended, the click shows nothing new.
  - A conflict panel opened by a read takes focus while the operator is typing; since round 17
    "Overwrite anyway" is four Tabs away, not two. Keys meant for the editor can still land:
    Shift+Tab (dedent) then Space presses Cancel and drops the draft; three Tabs then Space
    presses Reload. Neither writes. Changing that is a design call.
  - A read that closes a conflict panel while focus is inside it drops focus to `<body>`. A fix
    needs to know whether focus was in the panel, so it is a new mechanism.
  - The panel's "N unchanged lines" bar opens by click only (`@codemirror/merge`'s collapse
    widget). Predates the branch.
  - Text and sha can still come from two file versions in two cases no person can reach: another
    writer puts the old bytes back before Reload's re-read lands (A-B-A), or a read lands and
    Edit, a key and Ctrl+S all follow within @uiw's 200 ms typing latch. Both predate the branch.
  - Naming: a Save that writes the same bytes still leaves Apply off with no line when two rules
    share a query, two replace rows share a pattern, a custom rule is named `default`, `comp`
    or `singleton`, a base template the file lacks is cleared (the read fills beets' default),
    or every replace row is removed from a file with no `replace:` block. A rule with a template
    and no query, or a query and a blank template, leaves Save off with no reason; Save would
    write nothing for either.
  - Naming cannot remove a row that is on disk but that Save drops (a custom rule with an empty
    template or query, a replace row with an empty pattern): removing it is not a change, so
    Save stays off. The Beets editor can. Since round 17.
  - Naming drops a draft without a word when a read brings a new file version: the panel
    remounts on the file's sha (`NamingPanel.tsx`, `key={data.sha256}`). The Beets page opens
    the conflict panel instead. Predates the branch.
  - The tests' timed waits cannot fail correct code, but a mutant kill behind the 300 ms wait
    past @uiw's typing latch can pass on a slow run: the latch is 200 ticks of a 1 ms interval,
    measured 211-220 ms idle and about 450 ms with a busy event loop.
  Search words: conflict panel, Save button, round trip, draft lost, focus, Apply 409, Tab,
  Overwrite, Cancel, same bytes, A-B-A, typing latch, unchanged lines.
- **The config view hides a secret by its setting name, not its value** (security seat,
  2026-09-21, measured; owner: match beets, record it). A secret copied elsewhere with a YAML
  anchor or merge key (`other: {<<: *sub}`, `note: *alias`) shows in plain text, as it does in
  `beet config`. Only the operator can write that. No fix planned. Search words: redact, anchor,
  alias, merge key, secret, mask.
- **`GET /api/config` can fail once while an Apply swaps the config** (code-review seat,
  2026-09-21: about 7 failed polls in 1200 Applies against a nonstop poll). One confuse lookup
  reads the source list once, so single reads are safe; `flatten()` makes many lookups and can span
  the swap. The next poll recovers. Not fixed. Search words: flatten, swap, effective view, Apply.
- **A metadata-source lookup racing `load_plugins` can still cache a partial answer** (security
  seat, 2026-09-21, reasoned, not measured). `setup.open_beets` clears the `functools.cache` after
  `load_plugins`; a lookup that computed its answer before the clear and stores it after keeps it
  until the next Apply. Needs an album page's missing-tracks report inside an Apply plus a thread
  switch between two bytecodes. Search words: metadata_plugins, functools.cache,
  find_metadata_source_plugins.

- ~~**`/import`'s feed row starves its title exactly like the two `/review` rows did**~~ —
  **CLOSED 2026-09-11** (on `fix/phone-width-rows-and-hit-areas`; PR + squash sha cited at
  merge). The owner applied decisions 39 to this feed; the remedy is the decision row's, with
  the threshold re-derived from this row rather than copied. Its fixed content measures
  **285.37px** (px-4 16 + cover 40 + gap-3 12 + "Already in library" 107.98 + gap-2 8 + gap-3 12
  + Resolve 73.39 + px-4 16), so the floor is **296.70** (+ the 11.33px ellipsis glyph) and the
  ceiling is the **473px** narrowest desktop row, at the 768px sidebar step — the decision row's
  numbers to the pixel. The switch is **28rem**, the owner's number (decisions 41, 2026-09-11), now the same
  on all three rows: the floor stays true AS a floor, but it is not what sets the threshold.
  At the 20rem this round first shipped, the real phone band went inline and the title
  collapsed — measured on the widest parked row, viewport 392/400/414/430 gave
  **42/50/66/74px** of title (6/7/10/10 characters of 46); at 28rem the same widths give
  **127/135/151/159px** (18/19/22/23 characters), because the action is on its own line there.
  Measured switch: the action is dropped up to viewport **512** (row 447) and inline from
  **513** (row 448 = 28rem exactly), on the feed row and on both decision rows; the bank row
  switches at the same 512/513, unchanged. The 473px ceiling clears 448, so no desktop row
  ever drops. `FeedRow` is `ImportPage.tsx:860` on the branch (`:856`
  below was its line when this was found).
  Re-swept 320→1920 in steps of 8 with a real 15px scrollbar, six feed rows covering every
  status: the title is **never 0** (38px minimum, against 0 at six (width, row) samples before).
  The other three properties were already clean on this page and stay clean, which is what the
  sweep is for: no control clipped at any width, no text ink answering `elementFromPoint` with
  a control, no pixel of the meta span's box clipped. **Only a row with a button becomes a
  grid** — the
  other four rows are rect-for-rect identical at all 201 widths **row-relative**, by
  construction and measured. In PAGE coordinates they move down at every width where the
  action is dropped (320→512, 25 widths), because the parked rows above them grow: 28/48/68/88px
  at 320→384, read by the review seat on the 20rem build; at 392→512 by what those two rows grew
  (each 88→132px at 392, 88→112 at 512, re-read by the UX seat after the 28rem move), not re-read in page coordinates.
  Above the switch (viewport 513 and up, row ≥ 448) the two parked rows are identical to before
  in every measured property except AlbumRow's own wrapper box, which is narrower by exactly the
  action slot it no longer holds (80.62px for Review, 85.39 for Resolve) and paints nothing.
  The live log is undisturbed: with a row transitioning into a parked status at 320, the
  `role="status"` announcer makes the same two announcements as before (MutationObserver over
  the whole run), focus does not move, no scroll event fires and `scrollY` is unchanged —
  measured both at the top of the page and scrolled to the bottom, on both builds. The feed's
  `<ul>`/`<li>` semantics and the announcer's attributes are unchanged.
  Below is the original entry.

  **`/import`'s feed row starves its title exactly like the two `/review` rows did** — found
  2026-09-11 while closing decisions 39, NOT fixed, and it corrects a premise that has stood
  since #220: "`/import`'s feed row was clean" was about ink over a control, not about the
  title. Measured in `/import?job=…` at 320→344 with a real 15px scrollbar, on the widest badge
  the feed renders ("Already in library"): the title is `clientWidth` **0 at 320, 328, 336 and
  344** on the `needs_dup_resolution` row and **0 at 320** on the `needs_review` one — the same
  cause as the bank and decision rows (fixed content wider than the row, badge `shrink-0`, title
  `min-w-0`). `FeedRow` (`ImportPage.tsx:856`) has the decision row's exact anatomy — AlbumRow
  plus one action — so the remedy is the one already applied there: the row becomes a grid and
  the action takes row 2 below a measured threshold (the decision row's was 20rem, and the owner has since set 28rem on all three rows — closure
  above; this row's
  action is `Review`/`Resolve`, so re-derive rather than copying the number). NOT done here
  because decisions 39 names `/review`, and extending a ruling to a third surface is the owner's
  call, not an application of it.
  (Re-measured 2026-09-11 while fixing it, and the count differs: the sweep found a **sixth**
  sample, (328, `needs_review`) — the number the closure above carries. Both were true as
  written. The `needs_review` row's fixed content is 22.61px narrower than the
  parked-duplicate row's (badge 90.13 vs 107.98, action 68.63 vs 73.39), so its title's zero
  crossing sits near the 328 step and the two runs fall either side of it. Neither run
  recorded the crossing WIDTH, so re-measure rather than trusting five or six.)

- **`GET /api/albums/{id}/cover` answers 500, not 404, when an album has no `artpath` and
  its first track's file cannot be parsed** (found 2026-09-12 with a stand-in file in a
  scratch library; pre-existing, not the reset branch's code). `_cover_from_embedded` in
  `app/beets/library.py` checks `isfile` and then calls `MediaFile(track_path)` unguarded, so
  a truncated or wrong-extension file raises `mediafile.exceptions.UnreadableFileError` out of
  the route, while the `artpath` arm degrades to `None`. The grid still shows its placeholder
  (the `<img>` error path), but every paint of that album logs a traceback and the thumb cache
  records no miss. Fix shape to verify: catch `UnreadableFileError` around the parse and
  answer `None`, pinned by a track file that is a few bytes of text.

- **A focused action on an `/import` feed row unmounts under the user when the live feed
  applies that album.** `FeedRow` (`ImportPage.tsx:860`) gets its button from
  `feedRowAction` (`:823`), which returns one only for `needs_review` /
  `needs_dup_resolution`; the poll that moves the row past either status makes the action
  `undefined`, React unmounts the focused control, and focus falls to `<body>` — a keyboard
  user loses their place mid-page. The dropped action is `Button size="sm"` = **32px** tall,
  which clears decisions 40's 24px minimum but not 44px, and `frontend/src` declares no
  `pointer: coarse` floor anywhere. **PRE-EXISTING** — the button lived in `AlbumRow`'s slot
  on main and unmounted the same way — and **UNMEASURED**: read off the code, not reproduced
  in a browser by anyone yet. The branch's focus check covered a row GROWING a button, not
  one losing it.
- **Asymmetry to check: `/import`'s "Import finished" panel passes no `readOnly`; the failed
  panel does** (`ImportPage.tsx:1053` vs `:1145`, whose comment gives the reason — a decision
  POST no worker will consume). Rendered with `phase: "done"` plus a parked album, the done
  screen shows live Review/Resolve links and its summary line does not mention the parked
  rows. Same on main. NOT verified that the backend can report `done` while an album is
  parked — check that first: if it cannot, this is unreachable rather than a bug.

- ~~**`/browse`'s facet checkboxes get 20×24 of the new 24×24 tap target**~~ — **CLOSED
  2026-09-11** (on `fix/phone-width-rows-and-hit-areas`, PR #221, squash `a053ffc` = v0.51.2; vault
  decisions 40 — the ruling names the primitive and says every caller inherits it, so a call
  site that defeats it is inside the ruling). **Measured cost: none.**
  The remedy is NOT the `-ml-1 pl-1` recorded below, which costs 4px of every facet label
  (190→186) and shifts the album grid (518→514) because `md:w-60` is a border-box width and the
  padding comes out of the content. It is that pair PLUS `md:w-61` (15.25rem = `w-60` + the 4px),
  so the fixed width pays for the padding: `pl-1` moves the CLIP 4px left, `-ml-1` gives the 4px
  back to the layout, the width keeps the content box at 224px. The rail is
  `BrowsePage.tsx:364` on the branch (`:346` below was its line when this was found).
  Re-swept 320→1920 in steps of 8 with a real 15px scrollbar, against the same sweep on the
  merge base: the facet label's box, the legends, the album grid's box and grid-template, the
  rail's vertical scrolling, the rail's content width and the absence of any horizontal
  scrollbar (rail or document) are **identical at all 201 widths**. Screenshot pixel-diff at 390
  and 1280: 112 and 363 pixels differ, **max channel delta 1 and 2 of 255** — antialiasing on
  the moved box edge, nothing visible; not "identical".
  Target after, `elementFromPoint` walking out from the centre on the first, a middle and the
  last facet checkbox at every width: **24.5×24.5 with 4 of 4 corners** at 600 of 603 samples,
  against 0 of 603 before (base was 20.5×24.5, 2 corners, at every one). The 3 exceptions are
  the LAST checkbox at widths 896/1408/1464, where it is 24.5×**24.0** with 2 corners: with the
  rail scrolled to its end that box's bottom edge lands on the scroll container's own padding
  edge at a half-pixel boundary. It still meets the 24×24 minimum, it is **identical on the
  merge base** (same three widths, same 24.0), and it is a vertical clip this fix does not
  touch — closing it means 4px of padding at the rail's bottom, which moves the rail's own box.
  Below is the original entry.

  **`/browse`'s facet checkboxes get 20×24 of the new 24×24 tap target** — found 2026-09-11
  while closing decisions 40, NOT fixed. The primitive's pseudo-element reaches 4px past the
  drawn box on every side, but the facet rail (`BrowsePage.tsx:346`) is
  `max-h-72 overflow-y-auto` and its content box starts at exactly the checkbox's left edge, so
  the left 4px is clipped — `overflow-y: auto` alone makes `overflow-x` compute to `auto`, which
  clips. Measured with `elementFromPoint` at 390px: **20×24**, against 16×16 before and 24×24 at
  every other call site. Not a regression and not a neighbour collision (the 4px is page margin,
  and the associated `<label>` beside it is 277px of target for the same control); the row pitch
  is 26px, so SC 2.5.8's spacing exception is met either way. The fix is 4px of room inside the
  clip — `-ml-1 pl-1` on the aside keeps every facet row's ink where it is — and it is a layout
  change on a page the ruling does not name.

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
  destructive version of the same typo. **Measured again 2026-09-19 on a POPULATED library:**
  a source that is a PARENT of the music folder (or the beets dir, which holds `music/`) makes
  beets walk the library's own album folders as candidates — re-tagged by whatever the lookup
  returns, files MOVED to the new tag's path, the old folder pruned, the old row dropped by
  `remove_replaced` — under `copy` as well, because an in-library source is force-corrected to
  `move`; the real download is then set aside as a "duplicate" of the album just manufactured
  from the user's files. `is_in_library_source` asks "source inside library"; the missing
  question is "library inside source", one predicate. Which side dies is decided by the walk's
  lexical order (security seat, measured): with the library folder sorting first, the library's
  own album is applied at 29% confidence, its files moved, its folder and artist folder pruned,
  its row dropped, and the real download is rejected as that album's duplicate; with the
  download sorting first, the download imports and the library's own folder lands in the review
  queue as a set-aside pointing inside the library. (2) There is no cheap pre-flight: nothing reports
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

  **Added 2026-09-08: `sonarjs/no-alphabetical-sort` (S2871), taking the gate to 27 rules.**
  The family's whole life was one branch — `api/issues/search` reports 0 S2871 against
  `musicdrop` ever, resolved or open — so lock-on-clear applies to a family that never reached
  `main`. The twin is NOT `@typescript-eslint/require-array-sort-compare`, the obvious pick:
  S2871's `implementation` is `original` with `eslintId` `no-alphabetical-sort`, so SonarJS ships
  its own rule and that IS what the analyzer runs. The typescript-eslint rule is wrong in both
  directions, measured on the offending line: at its default (`ignoreStringArrays: true`) it
  reports NOTHING, and with that option off it has no equivalent of Sonar's
  `isSortUsedForNormalizationComparison` exemption, so it would be stricter than the server.

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

- ~~**`GET /api/config` serves the user's raw `config.yaml` — every credential in it,
  unmasked — to any caller who can reach port 3030.**~~ — **CLOSED by the auth slice
  (PRs #199, #200 and #201), 2026-08-29 to 2026-08-30.** The entry's own text named the
  remedy — "Auth is the fix … there is no sensible in-place patch that preserves the
  round-trip editor" — and that is what shipped: the session gate now covers every
  `/api/*` route, so `yaml_text` is served to a signed-in caller and nobody else. The
  route's body is unchanged and deliberately so; what changed is that there is now a
  caller identity. Original text kept below for the record.
  (Found 2026-08-28, auth-posture audit.) `yaml_text` is documented verbatim as the raw on-disk text with "secrets are NOT
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
  ids use (`WebImportSession._seed_replace_from_directive`; true when this shipped — since
  2026-09-18 on `feat/import-keep-downloads` the hook disposes of its own duplicates before
  placement and that pass serves this banked route only); `merge` cannot be forced at all
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
  **FIXED** on `fix/undoable-deletes` (PR #208, squash `6a5427c` = v0.48.0; follow-up from #189). Every mover records the origin
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
  than a cosmetic one. **ONE came back on 2026-09-13, on a different premise, and it brought a
  new gate with it:** the 64 KB cap (the same number), plus a `stat`-before-open the sidecar
  never had — it is not on the deleted list above. The premise: "this app owns the store
  directory" is not "nothing can be planted there" — an operator can point the store at an
  attacker-writable path, and a FIFO at a record's key hung `GET /api/trash` for the life of the
  process while a 600 MB sparse file at one cost +1199 MB RSS in one read, paid once per
  top-level entry and released between them (security seat, fix round 2 and 3). Round 4 closed
  the same two on the DELETE side, where the module's other reader had kept a bare `read_text`,
  and made the cap bound the read rather than only `st_size` — a procfs file is `S_ISREG` with
  `st_size == 0` and read 162,801 bytes past a 64 KiB cap with no race to win. Both readers now
  share one gate. What bounds the reach now is the layout row `music contains origins`.
  Round 5 (2026-09-14) reshaped that gate three more times. The `stat`-only gate is GONE, in
  favour of one `O_RDONLY|O_NONBLOCK` open with `fstat`, the size pre-filter and the bounded read
  all on that descriptor: a stat gate bounds what and how much is read, never how long, and a
  write lease on an ordinary 73-byte record at the key blocked any other `open` for 45 s. Round 6
  put a `stat` back IN FRONT of that open, deciding only whether to open at all, because round 5
  opened whatever a link at the key led to, a device included, before `fstat` refused it. A name
  that stats as anything but a regular file is refused unopened; the descriptor must be the inode
  the `stat` saw, and a different one is kept on the delete side (the store's own `os.replace`
  makes one). The DEVICE-OPEN window is narrowed, not closed: a link to a device swapped in after
  the `stat` is still opened before the `fstat` refuses it. **The fix shape, not taken**
  (security seat Low, fix round 6, measured by that seat): open the key `O_PATH`, `fstat` it,
  then reopen `/proc/self/fd/<n>` with `O_RDONLY|O_NONBLOCK` for the bounded read. The `O_PATH`
  open types a device without opening it and still follows an operator's link to a real record,
  and the reopen still answers EAGAIN under a lease. Linux-only, needs `/proc`. Adding
  `O_NOCTTY` to `_RECORD_READ_FLAGS` is the partial step: it stops a tty becoming the
  controlling terminal and does nothing for other devices. Inert with the container's default
  `/dev`. The
  delete side then stopped acting on every refusal — "the bytes are not ours" unlinks, "the store
  could not answer" keeps the file, because on a truncated key two entries share the loss was the
  OTHER entry's exact restore. And the three parse arms that unlinked in silence now log, so the
  most ordinary plant there is (a non-JSON ASCII file) leaves a trace. The RSS figure above is one
  of two measurements of the same ratio: a sparse file costs about twice its size, measured at
  600 MB → +1199 MB and at 400 MB → +801 MB.
  **Two residuals from that round.** A NON-EMPTY directory planted at a record's key still burns
  its Trash name for good: `S_ISREG` refuses it, `unlink` raises `EISDIR`, and the `rmdir` added
  for the empty case answers `ENOTEMPTY` — a function that runs after the entry is already gone
  may not start deleting trees it knows nothing about, so `origin_recorded` keeps answering "taken"
  and later albums land on `<name> (1)`, `(2)`… A key the store cannot ANSWER for (EACCES, a live
  lease) is kept by design and holds its name the same way until the fault is cleared. So a
  planter who can cause that fault (a lease, a mode-000 key as non-root) keeps a plant and burns
  that Trash name for as long as it lasts: the accepted cost of keeping rather than risking another
  entry's record (security seat I-1, 2026-09-14).
  **And one decision:** `trash_manage._link_name_under` no longer asks where a symlinked directory
  points. `realpath`-then-`walk` on the same name leaked a path from outside the entry into the
  503 in 8.01 % of restores under a flipper (3,162 of 39,486), so every symlinked directory is now
  named by its link; the cost is precision, not refusals.

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

  **That one exception sits outside the ruling's own reason, and the owner settled it**
  (2026-09-02: *"Keep them disabled"*, `decisions.md` 28 item 1). The amendment reasons that
  disabling Restore *"would have deleted a working recovery path in the name of safety"* —
  true of an `"import"` row, where the re-import IS the recovery path and works. A symlinked
  row has no such path: its files never moved into Trash, so restoring it would import
  whatever the link points at rather than undo anything. At this tip (2026-09-02)
  `resolve_trash_child` answers 404 to the Restore route AND to the per-row Empty route, on
  the link itself and before either route moves or removes anything (measured; pinned in
  `backend/tests/test_trash_listing_symlink_rows.py`), so the only reachable outcome of
  either live control was an error, and disabling them deletes nothing. That was NOT true at
  the previous tip, where the routes resolved first and a link to a sibling entry got acted
  on — so the carve-out rests on this tip's guard, not on a property the row always had. The
  standing record is the Accepted-residuals entry of the same name.

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
  (PR #208, squash `6a5427c` = v0.48.0)
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

- ~~**"Save art to library" is a one-click, unconfirmed action that permanently deletes
  hand-placed `artist-poster.*`/`artist-background.*` — and fires as rename collateral.**~~ —
  **CLOSED 2026-09-11** (on `fix/art-apply-keeps-hand-placed-art`; PR #224, squash `a8b08d5` =
  v0.51.4). Owner's shape: confirm + Trash, and a rename writes only where missing. The Apply
  sits behind an AlertDialog ("Save art to library?" / "Writes artist-poster and
  artist-background files into this artist's folders. Existing ones move to Trash first."),
  the files a forced write replaces go to one Trash entry per artist folder
  (`Trash/<folder> - artist art/`, origin record `moved="items"`, `trash_replaced_files`),
  nothing is written into a folder whose old files did not all move aside, and both rename
  call sites pass `force=False`; the rename dialog names the art write only when the toggle
  is on. Browser-verified on a scratch library: the seeded poster's mtime is on the Trash
  copy, the folder holds the new write, the origin record names the folder. Side fixes the
  review rounds forced: unpredictable fixed-length temp names created `O_EXCL|O_NOFOLLOW` in
  both the art and the lyrics writer (a symlink at the derived `.<name>.tmp` was followed and
  published as the destination — measured), the Trash container claimed by a bare `mkdir`,
  a per-artist store check on the runner, and a 10 s bound on the start request so a stalled
  start cannot latch the dialog. Not verified on a real cross-device move: the mid-copy
  failure is simulated by patching `shutil.move`. Residuals recorded below and under
  *Accepted residuals* (the Trash row's wording for an art container, the killed-write
  dotfile, the three derived-name writers left, the per-artist check window).
  (Original finding, 2026-08-28.) `POST /api/artists/art/apply` ran `force=True`
  (`app/api/artists.py:931`), and `_write_one` under force unlinks EVERY existing
  `artist-<kind>.*` before writing MusicDrop's resolved image
  (`app/beets/artist_art.py:82-84`) — the exact Plex Local Media Assets filenames a Plex
  user curates by hand. Deleted, not trashed, behind a bare `IconAction` that reads as
  additive; every other destructive action in the app sits behind an AlertDialog. The same
  `force=True` fires automatically on a rename when items moved and the write toggle is on
  (`artists.py:266-272`) — so merging artist A onto B silently replaces B's curated poster
  with whatever Deezer resolved, and the rename dialog never mentions art.

- ~~**`write_artist_art` reports `status="written"` when only some folders wrote**~~ —
  **CLOSED 2026-09-11** (on `fix/art-apply-keeps-hand-placed-art`; PR #224, squash `a8b08d5` =
  v0.51.4), as a side effect of the Trash move-aside: `_write_folder` returns
  `(written, failed)` per folder, `status` is `failed` as soon as one write or move-aside
  errored while `written` still counts what landed, and the model comment defines `failed`
  that way. Pinned by `test_a_failed_second_write_keeps_the_count_of_the_first` (poster moved
  and written, background write refused → `failed`, `written=1`). The job tally counts by
  status only, so a partial run lands in `failed`, not `written`. (Original finding,
  2026-08-28: the per-directory loop caught `OSError` into a `failed` flag the status ladder
  consulted only when `written == 0`, so the partial case had no representable value.)

- **`playlists_reexported` counts playlists whose `.m3u8` write FAILED.** (Found 2026-08-28,
  split out of the entry above when it closed.) The counter comments in `models/rename.py`,
  `models/edit.py` and `models/duplicates.py` all say "a failed write still counts" — the
  export swallows every exception. The honest shape is written vs attempted.

- ~~**"Reset to auto" in the artist-image panel deletes a hand-uploaded portrait with no
  confirm, and nothing refetches it.**~~ — **CLOSED 2026-09-11** (on
  `fix/reset-to-auto-confirms-and-moved-aside-trash-rows`, PR #225, squash `9e918e6` =
  v0.51.5).
  Reset is behind an AlertDialog in the Save-art shape — "Reset to auto?", and a description
  that warns an uploaded or pasted image moves to Trash — and the POST is sent from the
  dialog's action alone, held open until it settles. The endpoint moves the `*.override` pair
  to `Trash/<artist> - artist image` via `trash_replaced_files` before clearing any slot, and
  answers 503 when that store is refused or unwritable — the reset stops, and a store refused
  before the first move leaves the override untouched. The design call landed on "yes, an app-cache file may feed
  the Trash listing", with its own row kind rather than the album words — the entry below.
  Pinned by `tests/test_artist_image_reset_to_trash.py` (7) and six confirm tests in
  `ArtistImageEditPanel.test.tsx`. (Original finding, 2026-09-11 by the final code seat on
  `fix/art-apply-keeps-hand-placed-art`: Reset reached `ArtistImageCache.clear_override`,
  which unlinked the override bytes and mime sidecar with no dialog and no Trash, while
  README said of exactly those files "which nothing refetches".)

- ~~**The Trash row for an art container reads as an album.**~~ — **CLOSED 2026-09-11** (on
  `fix/reset-to-auto-confirms-and-moved-aside-trash-rows`, PR #225, squash `9e918e6` =
  v0.51.5).
  The wire says it now: `trash_replaced_files` records `moved="files"`, `_restore_fields` maps
  that to a fifth `restore_mode` — `by_hand`, with `_MOVED_ASIDE_NOTE` and the origin — and
  `restore_album` short-circuits such an entry to `could_not_restore` without running an
  import. The row reads "Files moved aside.", the backend's note, "Was at <origin>", with no
  Restore button at all and an Empty whose confirm says "these files" instead of "this
  album's files". A row with no artist and no album is titled by its folder alone, rendered
  once (the subtitle no longer repeats it), which also drops the invented "Unknown artist - "
  from the husk and symlinked-entry rows. Pinned by four new `SettingsTrashPage.test.tsx`
  tests plus the listing/restore pins in `test_trash_manage.py`. (Original finding,
  2026-09-11, browser-measured: the container listed with the album row's words — an
  "Unknown artist - " title, the shared-folder "Approximate restore." note, and a Restore
  button whose only outcome was `could_not_restore`.)

- **A `moved="files"` Trash entry has no put-back — the user copies files by hand.** (Named by
  both implementers on `fix/reset-to-auto-confirms-and-moved-aside-trash-rows` and by all three
  review seats, 2026-09-12; filed here rather than left in the reports.) `restore_album`
  short-circuits this shape to `could_not_restore` and the page renders no Restore, so the only
  exit is the note's instruction: copy the files out of the entry into the folder `origin`
  names. For a replaced portrait that means preserving an opaque `<sha1>.override` filename —
  README says so, but a button would not need saying. Fix shape: a sibling of
  `move_back_target` for this shape (it returns `None` on anything that is not
  `moved="folder"`, deliberately, before its containment test), whose target is accepted only
  when `record.origin` resolves inside the music library OR inside one of
  `config.app_cache_dirs`; then move each file in the container back by name, refuse rather
  than overwrite when a file of that name is already there, and report per-file. No beets
  import on this path at all — these are not media. Both halves stay honest only if the
  sanitizer's `origin_file` key rules are re-read first: the container name is client-derived
  for the artist-image caller.

- ~~**A write killed mid-flight leaves a `.<pid>.<16 hex>.<ext>.tmp` dotfile nothing clears.**~~ —
  **CLOSED 2026-09-12** (on `fix/descriptor-anchored-library-writes`; PR + squash sha cited at
  merge). One sink does it for every writer: `write_atomic_bytes` / `write_atomic_text`
  (`app/playlists/atomic.py`) sweep the target's own directory on each write, unlinking only
  regular files whose name matches this writer's shape (`_TMP_NAME_RE`,
  `.<pid>.<16 hex><suffix>.tmp`), never following a link, and only when `st_mtime` is older than
  `_STALE_TMP_AGE_S` (3600 s). The art and lyrics writers' private recipes are deleted and both
  call the sink. Measured cost, tmpfs, one 8-byte write: 0.020 ms into an empty directory,
  1.385 ms into one holding 10 000 files (~0.14 µs per entry). Mutants that each fail
  `test_the_sweep_removes_only_this_writers_own_stale_temps`: dropping the age gate, widening
  the regex to `^\..*\.tmp$`, dropping the `S_ISREG` check, and statting with
  `follow_symlinks=True`. Residual recorded below: old-shape leftovers and `artwork/cache.py`'s
  own temps are never swept.
  (Original finding, 2026-09-11 on `fix/art-apply-keeps-hand-placed-art`.) The derived `.<name>.tmp` was
  cleared by the next write of the same file; the unpredictable name that replaced it (art
  and lyrics writers) is not, so a process killed between the create and the `os.replace`
  leaves one dotfile per kill in the artist folder or beside the track. One per kill,
  invisible to beets, but it accumulates in the library. Fix shape: on the next write into
  the same directory, unlink `.*.tmp` entries that match the writer's own pattern and are
  older than a job could run — bounded to the pattern, never a bare glob. `playlists.atomic`
  carries the same residual.

- ~~**The move-aside and both library writers act on NAMES after checking them — three measured
  windows that only descriptor anchoring closes.**~~ — **CLOSED 2026-09-12** (on
  `fix/descriptor-anchored-library-writes`; PR + squash sha cited at merge), by the fix shape the
  entry named. All three windows are held by descriptors, so the "Until then the docstrings say
  'window', not 'cannot'" clause below is retired for these three. The READ and DELETE halves
  followed in the same branch's fix round: the security seat measured the lyrics marker delete
  running by path — a symlinked album folder had its sidecar deleted through the link before the
  anchored open was reached, and the content gate lost 17 111 of 67 479 atomic-retarget races — so
  `_present_sidecars`, the marker read, the clear and `artist_art.has_background` now resolve
  names against the same descriptor as the write (0 of 55 911 after). The last by-NAME reader went
  with the fix round: `_has_sidecar` decided the `skipped_existing` early return, which skips the
  FETCH rather than feeding a count, and it disagreed with the write in both directions (a dangling
  link at a sidecar name re-fetched that track every run and never wrote it). `_sidecar_present`
  asks through the album folder's descriptor instead. What still acts by name is `get_artist_dirs`,
  which compares spellings.
  (1) `trash_replaced_files(names, *, src_dir_fd, …)` takes bare names in the caller's own
  descriptor and every `rename` goes `src_dir_fd=` → `dst_dir_fd=`; what arrived is lstat'd
  through the CONTAINER's fd and renamed back if it is not a regular file or a symlink. Mutant:
  neutering that lstat fails `test_a_directory_swapped_onto_a_guarded_name_is_put_back_and_refused`.
  (2) The container is claimed twice — `os.mkdir(dest.name, dir_fd=trash_fd)` refuses anything
  that predates it, and an `os.open` plus an emptiness probe under it refuses a stranger's EMPTY
  directory renamed onto the name (`rename` replaces an empty directory, measured). The
  `moved == 0` arm unlinks its own children through its own fd and `rmdir`s only while the name
  still resolves to the inode it claimed; no `shutil.rmtree` (`rmtree(".", dir_fd=fd)` answers
  EINVAL, measured). Mutant: inverting the probe fails
  `test_a_stranger_that_takes_the_claimed_name_is_refused_and_not_emptied`.
  (3) `fsutil.open_below(root, rel)` opens EVERY component below the root
  `O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_NONBLOCK` and the art and lyrics writers create, publish and
  fsync through the fd it returns. Measured: the errno is **ENOTDIR**, not the documented ELOOP
  (ELOOP needs the absence of `O_DIRECTORY`), and a leaf-only `O_NOFOLLOW` on the whole path
  SUCCEEDS through a mid-path link — which is why the check is per component. Baseline before
  this branch, measured: both writers wrote outside the library through the link
  (`outside/artist-poster.jpg`, `outside/Album/01 t.lrc`). Now art answers `status="failed"` and
  lyrics `None`, each with one WARNING naming the folder; the root itself may still be a link
  (owner ruling 2026-09-12, `decisions.md` #45 — bind mounts are the supported spelling for
  spanning disks). Mutant: dropping `O_NOFOLLOW` from `BELOW_FLAGS` fails
  `test_a_symlinked_component_below_the_root_is_refused`.
  The EXDEV fallback the entry asked for is a descriptor copy carrying mode and `st_mtime_ns`,
  recreating a symlink from its own target and unlinking the source last, and it is now covered by
  a REAL cross-device move (`tests/test_trash_replaced_xdev.py`: Trash on `/dev/shm`, library on
  `/tmp`, the kernel's own EXDEV asserted; `MUSICDROP_REQUIRE_XDEV=1` in CI turns a
  same-filesystem skip into a failure).
  (Original finding, 2026-09-11 by the security seat on
  `fix/art-apply-keeps-hand-placed-art`; all Low, none exploitable for privilege.)
  (1) `trash_replaced_files` lstat-guards every file, then runs `require_usable_store`, the
  allocator and the container `mkdir` before the first `shutil.move` — first lstat to first
  move measured at ~0.1 ms; a directory renamed onto `artist-poster.jpg` in that window is
  moved whole into the container (rename needs write on the source's parent, so only trees
  already inside the library can be renamed in; the reach is "attacker-owned data relocated
  into Trash"). (2) The container `mkdir()` is the claim on the name only for what predates
  it: after it returns (mkdir to first move ~5 µs) a directory renamed onto the name (one
  syscall) is what `rmtree` removes on the `moved == 0` arm, and a symlink put there (`rmdir`
  + `symlink`, two) is followed by `shutil.move` — the file lands outside `trash_dir`
  (`rmtree` on a symlink raises, that half holds). Precondition: write on `trash_dir`, which the layout rule permits when Trash sits
  strictly inside the music dir. (3) `O_NOFOLLOW` guards the final component only: with the
  artist folder itself a symlink (`/music/Artist -> /data`), `_atomic_write_bytes` and
  `_atomic_write_text` create the temp file and `os.replace` INSIDE the target — measured
  `/data/artist-poster.jpg` written. Bounded to eight basenames (`artist-poster`/
  `artist-background` × the four allow-listed extensions) and needs an album imported through
  the symlinked spelling; preexisting on `main`, the branch made it safer (the move-aside globs
  the same directory, so a file there goes to Trash instead of being clobbered). Fix shape,
  one design: open the claimed container and the artist folder once
  (`O_RDONLY|O_DIRECTORY|O_NOFOLLOW`) and do every create, `rename`, `replace` and fsync
  through that descriptor (`dir_fd=` / `dst_dir_fd=`; keep a copy fallback for EXDEV) — the
  descriptor stays on the directory the check saw whatever the name does afterwards. Until
  then the docstrings say "window", not "cannot".

- ~~**Three writers still use the derived `.<name>.tmp` shape the art and lyrics writers
  left.**~~ — **CLOSED 2026-09-12** (on `fix/descriptor-anchored-library-writes`; PR + squash sha
  cited at merge). All three now call the shared sink and hold no recipe of their own, so the
  line citations below are stale: `config_editor.atomic_write` dumps the YAML and delegates
  (`beets/config_editor.py:374`, `mode=None`), `ArtistImageToggle.set_enabled` is one call
  (`artwork/toggle.py:44`, `mode=None`), `_write_artwork_atomic` is one call
  (`playlists/store.py:206`, `mode=0o600`). Each got a test that plants a symlink at its OLD
  derived name and asserts the link's target is untouched; measured on the old body, the write
  followed the link and `os.replace` published the LINK as the destination (playlists store: the
  JPEG bytes landed in `secret.txt`, which then took the `0o600` chmod, and the served artwork
  became a symlink to it). Mutant per writer: restoring the old derived-name body fails that
  test. Two behaviour changes came with the sink — the toggle file is now fsync'd (its own fd and
  the parent's; before, neither) and keeps a tightened mode across a flip, and all three inherit
  the stale-temp sweep. None passes `dir_fd=`: none of the three is under the library root, so
  the ruling does not reach them — residual recorded below.
  (Original finding, 2026-09-11.) `beets/config_editor.py:391` (config.yaml),
  `artwork/toggle.py:40` (the art-write toggle file), `playlists/store.py:204` (playlist
  artwork under the playlists store). A symlink planted at the derived name is followed and
  then published as the destination — measured on the art writer before this branch. All
  three write under app-owned paths (the beets data dir), not the attacker-writable music
  library, so a planter would already own what it could steer: lower reach, same remedy
  (`artist_art._tmp_path`: random fixed-length name, `O_EXCL|O_NOFOLLOW`) when each file is
  next touched.

- **The reset endpoint's cache-dir `os.open` failing between `override_files` and the move is
  untested.** (Found 2026-09-12 on `fix/descriptor-anchored-library-writes`.)
  `artists._trash_override_files:824` opens the artist-image cache dir at `:840` (following links,
  since it is app-owned) and hands `trash_replaced_files` the two bare names; an `os.open` that
  fails there lands on the same 503 arm as a refused origin store, and no test drives it, so
  that arm's body and "nothing moved" claim are unverified for this cause. Cheap: patch `os.open`
  to raise for that one directory and assert the 503 detail plus the override pair still on disk.

- **The three folder movers work by PATH at BOTH ends, so the swapped-root window closed for the
  move-aside is still open on the album and husk delete paths — and their source side is
  un-anchored too.** (Found 2026-09-12 on `fix/descriptor-anchored-library-writes` by the fix
  round; security seat M-1, corrected and widened by its round-2 seat.) `trash_album`,
  `trash_album_folder` and `trash_folder` (`app/beets/trash.py`) each `mkdir` the Trash and work by
  path — `_unique_trash_dest` probes the candidate name with `exists()`, then `shutil.move`
  (`trash_album` relocates per item with beets' `Album.move`). `trash_album_folder` and
  `trash_folder` receive `ProtectedTrees` and use it for the SOURCE guard, not for the root open;
  `trash_album` does not receive it at all, so plumbing it through its three call sites — one of
  them the import session — is part of the work. Those `mkdir`s are now no-ops on every path a
  request reaches: `checked_protected_trees` creates the Trash through the anchored walk first, at
  the delete ops, the three Trash routes, the artist-art store, duplicates' resolve and the orphan
  sweep. The ONE site where they still create it is the import session's post-import cleanup
  (`import_session.py:1774`), which runs `check_store_layout` and then `trash_album`, and holds no
  `LibraryHandle` to hand `checked_protected_trees` — giving it one, or a handle-free form of that
  call, is the rest of this item.
  Three measured facts the next reader needs:
  (1) **The destination.** Measured on the move-aside before its fix: `music/.trash` swapped for a
  symlink after `checked_store_dirs` put the container in `somewhere-else/` with the origin record
  naming an entry that does not exist. Same shape here, larger blast radius — a whole album tree,
  the library rows dropped in the same call, and a Restore with nothing to act on.
  (2) **The cheap half is an EDIT, not a rewrite.** `os.rename(name, name, src_dir_fd=,
  dst_dir_fd=)` relocates a whole directory tree in ONE syscall and answers EXDEV cross-device —
  the errno `fsutil.move_no_merge` already branches on (measured, seat probe `probe_q4c`). The
  exposed layout, Trash INSIDE the music library, is the same filesystem by definition, so the
  rename covers precisely the attacker-reachable case; only the cross-device whole-tree COPY is a
  rewrite.
  (3) **The source side is un-anchored too**, so a fix that anchors only the destination leaves it
  open: measured (`probe_q4b`), a symlinked component ABOVE the folder made `trash_folder`
  relocate a directory from OUTSIDE the music library into Trash. Reachability is a TOCTOU rather
  than a plain escape (`os.walk` does not follow links, so the sweep will not ENUMERATE through
  one) — the swap goes between the enumeration and the move.
  The shape that fixes every mover at once, destination and source: **the checked resolve hands
  back an open fd** rather than a path, so nothing downstream re-resolves either end. That is the
  same shape the Trash-root residual below wants, and what `empty_all` already relies on.
  Precondition is unchanged — write on the Trash's parent, i.e. a Trash configured inside the music
  library.

- **Boot accepts a Trash chain the Trash routes then refuse** (2026-09-13, PR #227; code seat W3,
  fix round 1; Apply half corrected 2026-09-14). The lifespan in `main.py` calls
  `checked_store_dirs`, which runs the layout rows on RESOLVED paths and not the anchored walk; the
  read routes (`checked_reachable_store_dirs`) and the writes (`checked_protected_trees`) add the
  walk. Measured on the operator-link shape (`/srv/x -> <M>/a`, `<M>/a` attacker-owned):
  `checked_store_dirs` ACCEPTED, `checked_reachable_store_dirs` REFUSED. So the container comes up
  healthy and hands the import registry that pair, which the post-import Replace cleanup checks with
  the rows only (the folder-movers entry above), while the Trash page, the reorganize previews and
  deletes answer 503. Apply does not accept it: step 2b (`config_editor.on_disk_layout_error` →
  `store_layout.layout_check_for_config`) runs the walk and answers 422 (the walk's refusal measured
  by the branch review 2026-09-14; the 422 read in `config_editor`). Step 4b's backstop checks the
  rows only, and meets a refused chain only if the chain changes between steps 2b and 4b, or if step
  2b reads `directory:` differently from what beets loads (an `include:` shape, a beets upgrade).
  The signal that exists: Settings → Beets runs the same read-only walk and paints the refusal on
  `directory:`. Left for the owner: refusing at boot is a behaviour change.

- **Restore's declared 503 says "setup fault" for a refusal that is not one** (2026-09-13,
  PR #227; code seat Suggestion 3, fix round 2). `_TRASH_LIBRARY_UNAVAILABLE_RESPONSE` in
  `api/trash.py` reads "The folder was not moved out of Trash; the message says which setup fault
  refused it.", and `restore_trash` maps `TrashEntryUnreadableError` (a pipe, socket or device in
  the entry, or the entry itself) to that 503. That is Trash content, and its remedy is Empty,
  not configuration; the comment above that arm calls the description true "word for word". Fix:
  drop "setup", then the two-step OpenAPI regen (`frontend/src/api/schema.d.ts` carries it).

- **The bank re-lookup, the import review's Rescan and disk sync open files by name with no
  regular-file gate** (2026-09-13, PR #227 security seats, code-read there). `research._read_items`
  calls `Item.from_path` on every name `os.walk` lists, any extension; it backs the bank's search
  and rescan routes (`api/bank.py`, `run_in_threadpool`, no lock) and the import session's Rescan.
  `disk_sync._probe_changed_fields` (the preview) and `_sync_item`'s `item.read()` (the job, for
  a file whose mtime moved) open a library track's path the same way. Measured here:
  `Item.from_path` on a FIFO named `pipe.flac`, and on one named `pipe.txt`, was still blocked
  after 3.0 s. So a FIFO
  in a banked folder parks one worker for the life of the process, and one at a track's path parks
  the preview's worker or the disk-sync job, whose slot keeps `raise_if_library_busy` answering 409
  (code read). Reach: write access in the inbox, a banked folder or the music library. Fix shape:
  the Trash listing's `_is_a_regular_file` gate, or the fd-shaped read its residual describes.

- **A pipe, socket or device directly in the Trash root has no row, and Empty all removes and
  counts it** (2026-09-13, PR #227; security seat, fix rounds 1 and 3; browser pass 2026-09-14).
  `_audio_free_entries` lists a top-level entry only when it is a directory or a link, and
  `_walk_trash_groups` skips a non-regular file; `empty_all` enumerates the Trash descriptor and
  `_remove_checked_entry` unlinks a non-directory by name. Measured here (a temp Trash holding
  `Real/` and a FIFO `loose.flac`): `list_trashed_albums` gave `['Real']`, `empty_all` gave
  `removed=2`, Trash empty. With only such an entry left the listing is empty, so the page says
  "Trash is empty" and hides Empty all (rendered only when `albums.length > 0`; code read), and
  Restore's refusal for an entry that is itself a pipe says "Empty removes it" about a row a
  refresh takes away. The regular-file version is already described in code, not here: a loose
  file at the Trash root that `Item.from_path` cannot read (a stray sidecar) is skipped by
  `_walk_trash_groups` and is neither a directory nor a link, so it "is listed by neither half"
  (`_audio_free_entries`' docstring, `trash.trash_replaced_files`'). Fix shape (seat's, not run):
  one `lstat` in `_audio_free_entries`' predicate, listing any non-hidden top-level entry not
  already grouped as a zero-track row, covers both (code read).

- **The slskd webhook's remap strips its prefix as text, so a Downloads path outside it imports
  nothing and reports no failure** (2026-09-14, providers research). `slskd.service.remap_to_inbox`
  uses `str.removeprefix`. Measured 2026-09-14 with prefix `/app/downloads`: `/app/downloads2/X` →
  `<inbox>/2/X`, and `/elsewhere/X` (not under the prefix) → `<inbox>/elsewhere/X`;
  `acquisition.inbox.contain(..., strict=True)` accepted both, and accepts a folder that does not
  exist (`Path.resolve()` is non-strict). The webhook then answers `queued`:
  `coalesce_album_root` returns a non-disc folder unchanged, `enqueue` passes (the ledger's `seen`
  is False for a folder it cannot stat), and the drain move-imports the missing path. Measured
  here with a real `BeetsImportRunner` (move, unattended) on a missing inbox folder: `on_finish`,
  no error. So the job ends `done` with nothing set aside, and the queue records `imported` and
  marks the ledger with an empty identity (code read). If a folder does exist at the remapped
  path, that one is imported instead. Why now: after a mount change, a Downloads path that no
  longer matches fails silently. Fix shape: match whole path segments, and refuse a path outside
  the prefix with one log line naming both paths.

- **A duplicate resolve that faults part-way drops the earlier albums' rows and keeps the
  rest.** (Found 2026-09-12 on `fix/descriptor-anchored-library-writes`; security seat L-2,
  pre-existing.) `resolve_duplicate_group` (`app/beets/duplicates.py`) wraps its loop in one
  `with lib.transaction():`, which reads as atomic and is not: with the second loser's
  `trash_album` raising, measured album rows `[1,2,3,4,5]` became `[1,3,4,5]` — the first
  loser's rows dropped, its files under Trash with an origin record, album 3 untouched, HTTP
  500. Recoverable, and the 500's body already says so ("Moved copies are recoverable in the
  Trash folder"), so this is a clarity-of-state bug rather than a loss. The real behaviour is
  now pinned (`test_a_fault_on_the_second_loser_leaves_the_first_one_dropped`) and the
  docstring says it. The option not taken: give each album its own transaction, so the state
  after a fault is per-album rather than ambiguous — it does NOT make the call atomic (the
  earlier album stays dropped either way), it only stops the ambiguity, which is why it was
  recorded instead of shipped. Whoever takes it should decide what the response should then
  say about the albums that DID move.

- **A bind mount of a library subfolder in the Trash's configured chain relocates the Trash
  outside the music library.** The symlink half of this entry is **CLOSED 2026-09-13** (on
  `hardening/link-targets`); what is left is the mount half, which no check here catches.
  (Found 2026-09-12, folded and re-measured 2026-09-13; security seats M-1 and H-1'.)
  `store_layout._below_the_music_root` and `_reaches_the_music_root`
  (`app/beets/store_layout.py`) ask whether the directory the walk REACHED is inside the music
  library, by climbing `..` through descriptors. From a MOUNT root, `..` crosses to the
  mountpoint's parent, so a bind mount of `<M>/a` at an outside path is never recognised as
  being inside the library: measured under `unshare --map-root-user --mount`,
  `_reaches_the_music_root(leaf)` answered False and two requests in a row put the Trash at that
  outside path, with `empty_all` enumerating it.
  **Trigger.** `MUSICDROP_TRASH_DIR` names a path that reaches inside the music library through
  a bind mount of a library subdirectory. Not the documented layout — compose recommends
  `MUSICDROP_TRASH_DIR=/music/.trash`, which is anchored — and the attacker cannot create the
  mount: it is an operator misconfiguration that the app then cannot see through. Under it,
  though, they regain the whole H-1' escape with ONE symlink: `below` never becomes true beneath
  the mountpoint, so the below-the-root link refusal is off for every component under it, and the
  Trash relocated to the attacker's own directory with no race and no rename (measured
  2026-09-13, before this branch and after it alike).
  **Blast radius**, bounded by the row layer, which runs against the RESOLVED destination
  (measured 2026-09-13): the origin store is refused, `/` fails on permissions, and the music
  root itself keeps `below = True` and so keeps the library-presence guard — but the beets data
  dir and an arbitrary outside directory were both ACCEPTED. What the app then does there:
  writes deleted albums and moved-aside art, keeps origin records that promise the moves are
  recoverable, and `rmtree`s the contents on Empty Trash.
  **The option left, described and not measured:** parse `/proc/self/mountinfo` and refuse a
  spelling that traverses a mount whose root is inside the music library. Linux-only (which this
  app is), ~20 lines plus a parser to keep correct, and it needs the mount id of each component
  the walk stands on (`/proc/self/fdinfo/<fd>`'s `mnt_id`, or `statx(STATX_MNT_ID)`) to be
  compared rather than a path, or it re-introduces the by-name divergence the walk exists for.
  **What the 2026-09-13 ruling closed, and why it did not close this.** The owner's question was
  *should `MUSICDROP_TRASH_DIR` be required to be spelled with no symlink in its path?* — answer
  no, with the link shape closed a different way (*"if its safer … do it"*). The walk now opens
  every component `O_NOFOLLOW` and resolves a link itself: `os.readlink`, the target walked hop
  by hop under the same rules, a shared 40-hop budget (the kernel's own, measured), and a refusal
  when the target lands inside the music library — the root itself excepted, which is the alias
  spelling and anchors the walk from there. The measured price was zero layouts: all 144 tests
  that existed before it across `test_trash_root_creation.py`, `test_config_store_layout_api.py`
  and `test_store_layout_use_sites.py` keep their outcome AND their message, including the three the
  blanket refusal would have cost (the outside Trash through the operator's link, the alias
  spelling, the search-only ancestor) and the `/tmp`-is-a-link hazard. Cost per destructive
  request, median of 300 walks: unchanged at 0.011 ms and 0.020 ms for the two shipped shapes,
  0.020 → 0.032 ms for a chain with one link above the root. That per-link cost is linear in the
  TARGET's component count — re-measured at +12 µs for a 4-deep target and +79 µs for a 12-deep
  one, ~3 ms for a 40-link chain — and the hop count is operator-controlled only, since a link
  below the root is refused instead of resolved. The shapes closed with it: security
  seat H-1' A3/A4/A5 (the operator's `/srv/x -> <M>/a` with the attacker owning or creating
  `<M>/a`) and J1–J5 (a link planted below the jump-in point). A link target component is opened
  `O_PATH`, which needs search alone, because that is all the kernel needed to resolve the same
  link; `..` inside a target is walked rather than collapsed, since `openat(fd, "..")` is the
  kernel's own answer for a descriptor the walk holds — and the walk re-asks where it stands
  after each such hop.
  **A `..` hop that LEAVES the library is refused, decided 2026-09-13 on the owner's criterion
  for this branch** (*"if its safer … do it"*, `decisions.md` #46): re-asking at the hop turned a
  stale-`below` refusal into an accept for a target that dips into the library and climbs back
  out, and the directory it climbs into is the library root's PARENT — attacker-writable wherever
  beets' `directory:` is a subfolder of a writable share, where the link waiting there was then
  resolved and the Trash created at its target with the library-presence guard skipped (security
  seat L-1', probes d1/d3). **Measured cost: six spellings a pre-branch install ACCEPTED are now
  refused** — p1, p3, c5, c8, e2 and e3, every one where the operator's own link target climbs
  out of the music library — re-measured 2026-09-13 at `v0.51.6`, the release `main` was at when
  this branch started, by the security and code seats independently. The note here used to say
  those shapes "the pre-branch code refused too", which is true only of a commit already ON this
  branch and reads as "the cost is zero": an operator whose Trash is spelled through such a link
  (`ln -s "$MUSIC/../trash-music" /srv/x`) goes from a working Trash to a 503 on every Trash
  route and both reorganize previews. A hop that lands ON the root (the alias spelling, p2) or
  stays below it is unchanged, and every store-layout test that existed before the refusal keeps
  its outcome and its message, except round 1's own test for the accepted shape, flipped to the
  refusal and renamed. The "164 of 165" this used to read was a count over the rounds' own
  three-file subset, which the sentence never named; collected across all five store-layout test
  files it is 256 of 257 (code seat, 2026-09-14).

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

- ~~**`MUSICDROP_TRASH_DIR` is an unvalidated `rmtree` root.**~~ — **SHIPPED — PR #215, squash
  `35d37bc` = v0.50.2** (2026-09-05).
  One table in `app/beets/store_layout.py` refuses the Trash or the origin store being or
  holding the music library, the beets dir, `library.db` or an app store, and the beets dir
  nesting with the music library. Asked at startup and at each destructive use site.

- ~~**The orphan sweep's ignore list does not protect an ignored dir's ANCESTORS.**~~ —
  **SHIPPED — PR #215, squash `35d37bc` = v0.50.2** (2026-09-05), same series. `orphans._drop_excluded_ancestors` folds
  each excluded root's ancestor
  chain into the drop, and `orphans._exclude_ids` drops (with one WARNING) an exclude root at
  or above the walk root. Exclusion is by inode, not by spelling.

- ~~**The layout predicate compares SPELLINGS, so an alias walks past it; and a refused
  Reorganize keeps the job slot.**~~ — **SHIPPED — PR #215, squash `35d37bc` = v0.50.2**
  (2026-09-05), found by the
  2026-09-04 review round of the two entries above.
  `app/beets/protected.py` re-asks by `(st_dev, st_ino)` at the moment a tree is moved or
  removed; `reg.start` runs after the store check, so a 503 no longer leaves `phase=running`.

  Residuals, accepted. This list is the one place they live; the modules point here.
  * A path that does not exist yet has no inode, so an alias onto a not-yet-created Trash is
    caught on the next check, not the first.
  * A mount point as the INNER path is caught at the mover, not by the predicate.
  * The set holds the protected ROOTS' inodes, so a Trash entry that aliases a SUBFOLDER of
    one is not recognised; the mount point itself survives `rmtree` with EBUSY.
  * Identity is `(st_dev, st_ino)` from two `os.stat` calls. A filesystem that synthesises
    inode numbers client-side (CIFS `noserverino`, some FUSE) could disagree between them —
    not measured, no such mount here.
  * A union filesystem (overlayfs, mergerfs) gives one directory two `st_dev`s, so a Trash
    spelled through a layer is walked by the sweep. Spell it through `directory:`'s mount.
  * A protected directory the guard could not LIST is logged, not refused: its own identity
    was compared from its parent's descriptor, an alias hidden below it was not.
  * beets' `prune_dirs` after a per-item move removes an EMPTY app store that is an ancestor
    of the album; a bind-mounted store answers EBUSY and survives.
  * The Trash and origin store reach the sweep RESOLVED, so the spelled-ancestor mechanism
    has no input for them: an ancestor of the CONFIGURED spelling can be reported as a husk.
  * A Trash inside a live album folder is not refused — one album, visible on the Trash page,
    and the check would cost a DB query per request.
  * `library:` in an audio-free subfolder of the music library stays a husk: `dirname(L)` is
    excluded from the sweep rather than refused.
  * A husk beside an excluded root is skipped when the ancestor's only audio sits INSIDE
    that root.
  * The seeds climb starts below a symlink where the library walk does not descend, so the
    two modes disagree on a symlinked subtree. Kept: seeds mode is what sweeps such a tree.
  * The playlist export dir at or above the music root is dropped from the sweep with a
    WARNING rather than refused.
  * The two image caches are participants; the static dir (`/app/static`) is not — it is
    served code, not data.
  * The Apply backstop's degraded state has no API field of its own; it is the 422's message.
  * The three movers (`trash_folder`, `trash_album_folder`, `restore_album`) guard by PATH; a
    swap after the guard lands in Trash, where the delete side refuses it by identity.
  * `delete_artist` pre-checks every album with a full walk and the mover walks each one
    again — the artist's subtree is stat-walked twice per delete.
  * No read deadline on an `include:` file: a dead hard-mounted NFS include blocks the
    worker in `os.read`, as it would block beets.
  * The `include:` read is a path existence/type oracle for an authenticated session, and it
    follows `~`. No content is disclosed; beets does not confine includes either.
  * A hand-edited `config.yaml` nested deeply enough answers a bare 500:
    `build_config_snapshot`'s `yaml.safe_dump` raises `RecursionError`, which no handler
    catches. Measured: 400 levels, default limit 1000. `GET /api/config` and
    `POST /api/config/apply` both return that snapshot. Authenticated, and nothing is lost.
  * One in-budget `POST /api/config/validate` costs ~1.5 s of threadpool CPU (32 includes
    summing to 1 MiB) and `/api/config/*` has no inbound rate limit. Accepted: the app is
    session-gated and single-operator.
  * Next touch of the destructive primitives: one `CheckedStore` (trash_dir, origins_dir,
    protected) so they take one keyword rather than three.

- ~~**A FLAT library layout defeats the delete path's presence check — it samples the music
  root against itself.**~~ (Found 2026-09-02, on `fix/undoable-deletes`, while re-reading the
  check that entry-above's sibling shipped.) —
  **FIXED in #209** (squash `5b7643b` = v0.49.0). Trigger: a
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
  "every album's folder", and its FIVE pins moved with it — not four, as this entry and the
  commit first said. Grepping the old wording at `6a5427c`: `test_library_presence.py` (one),
  `test_library_presence_sampling.py` (two), and `test_delete.py` (two — the verbatim 503 and
  the artist fan-out's 500, which is the file that was touched twice).
  **The trade, measured rather than argued.** Keying each slot to a FILE is stricter in
  exactly one shape: an album whose folder survives with its sampled track removed by hand is
  now a miss where the folder was a hit. At 200 albums with 1 file, 5 files or a whole folder
  removed it refused 0 times in 1000 draws each; at 2 albums with one file gone, 0 of 200; at
  5 albums with four gone, 0 of 200; on the singleton fallback arm, 0 of 1000. It refuses when
  ALL five sampled albums are missing their sampled file, which is certain where there is no
  other album to draw — N=1 with its only (or its lowest-named) track removed while the folder
  stays: 20 of 20 refusals against 0 of 20 before — and equally certain where every album is in
  that state, measured 50 of 50 at N = 1, 2, 6, 20 and 200. Between those it is the `f**K` rate
  `_PRESENCE_SAMPLE_SIZE` already models rather than a shape: with half of 200 albums missing
  their sampled file, folders intact and the share mounted, 33 refusals in 1000 draws (24 in an
  independent run of the same probe) against an expected 3.1%. A few missing files among many
  are not refused. The certain case is the shape `_PRESENCE_SAMPLE_SIZE`'s own note already
  documented as refused-by-design for a removed FOLDER at or below the sample size, extended
  to a removed file, and it is stated in `require_library_present`'s docstring and in README.
  The alternative that was NOT taken: skipping rows whose `dirname` is the library root, which
  leaves a flat library with an empty sample and the empty-sample arm accepts by design —
  i.e. it removes a flat library's presence check rather than correcting it.
  Regression: `test_library_presence_sampling.py::test_a_flat_layout_does_not_sample_the_music_root_against_itself`
  (the predicate, with the flatness of the fixture asserted from `Item.destination()`) and
  `test_trash.py::test_trash_album_refuses_a_dropped_flat_share_masked_by_a_stray`
  (end to end, so the ghost arm is what is proven guarded). Both were mutation-tested by
  restoring `dirname` + `isdir`.

- ~~**An unreachable origins store makes the allocator hand out a recorded name, and the next
  folder inherits the first one's origin.**~~ —
  **FIXED in #209** (squash `5b7643b` = v0.49.0), 2026-09-02, per the owner's
  ruling in `decisions.md` 28 item 3: the delete is REFUSED while the store cannot be used. What shipped, by symbol:
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
  album row already gone.**~~ —
  **FIXED in #209** (squash `5b7643b` = v0.49.0), 2026-09-02, per the owner's
  ruling in `decisions.md` 28 item 4, WITH a residual
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
  re-imports the folder, which is where it now is. The error does not claim the rows survived
  for that reason — the same exception type covers a `DBAccessError`, which raises BEFORE any
  row is written and leaves the album intact — so it names the step that failed, states the two
  disk facts it measured, and says it cannot tell whether the album is still listed. Pinned in
  both states: `test_trash.py::test_trash_album_folder_puts_the_folder_back_when_the_rows_will_not_go`
  (the DB shape) and `::test_the_move_back_says_nothing_about_rows_a_listener_already_took`
  (the listener shape, where the album row is measured gone and its 14 item rows remain).
  *(Both tests, the undo and the two exceptions went with the whole-folder album mover when
  this branch removed it — see "Delete moves an album's own files to Trash" under Next up.)*
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

- ~~**The candidate page overflows worst BETWEEN the breakpoints**~~ — **CLOSED 2026-09-08 with a
  container query on the panel, and 2026-09-08 (second pass) with a second one on `Field`.**
  The panel declares `@container/panel` and stacks its cover above the fields below 27rem of
  panel content; below 14rem the `Field` label takes its own line as well.
  **All widths below are the panel's CONTENT box with the scrollbar present, swept 320→1920 in
  steps of 8 (201 widths, one realistic fixture).** The first pass recorded `clientWidth` taken
  with scrollbars hidden and called it the content box, so its three first-pass panel widths
  (558/286/235 at 608/640/768) read **47/39/39px too wide**; they are 511/247/196 and are
  corrected here. The spread is not a constant: 32px of it is the `p-4` padding, and the rest is
  the scrollbar's effect on the LAYOUT, which differs by arm — at 608 the panels are stacked
  full-width so 15px of scrollbar costs the panel all 15, while at 640 and 768 `sm:grid-cols-2`
  has halved it so 15px of viewport costs ~7px of panel. The already-content-box figures moved by
  that same ~7px (203→196, 459→452). Container-query size IS the content box — verified
  with a `100cqw` probe, equal to `clientWidth` − 32 at all 201 widths.
  Side-by-side costs 208px (`w-48` cover + `gap-4`) plus 128px per `Field` (`w-12` label 48,
  `gap-2` 8, the "changed" badge 64 and its gap 8 — measured), so a value clears 96px (about 13
  characters) from 432px of panel up. The panel content box moves at **0.5px per px of viewport**
  wherever `sm:grid-cols-2` applies — NOT across the whole range: below `sm` the panels are
  stacked full-width and move 1:1, which is what this entry's own 520/528 pair shows (423→431,
  8px of panel for 8px of viewport, against 4px at 1224→1232). The `@min-[27rem]` arm first engages at
  a **1248px** viewport, where the panel is **436px** — it crosses 432 between 1240 and 1248.
  **Before the panel query**: a value was 0-width at 67 of the 201 widths and clipped at 69 more,
  and the document overflowed at 47 widths (320→960, max 100px — 72/32/49/100/68px at
  320/360/640/768/832). **After it**, no value was 0 at any width and document overflow was 0
  everywhere, but **7 values were still clipped, at 5 widths (768/776/784/792/800) across two
  fields** — the After panel's Album and Label. That was the `Field` row, not the cover: at the
  app's narrowest panel (196px of content at a 768px viewport, because the `md` sidebar has opened
  but `sm:grid-cols-2` has already halved it) the shared line left 67px for an 84px value.
  **After the `Field` query, 0 values are clipped and 0 are 0-width at any of the 201 widths** —
  for THIS fixture. That is not a property `Field` guarantees: with a realistic long artist the
  same sweep clips 532 of the 1608 measurements. The query removes the arithmetic squeeze, not
  every possible overflow.
  It changed 72 of 1608 panel×field×width measurements; the other 1536 are byte-identical, and
  every width that moved is 320 or 768–824. 1280 gives the panel 452px, so the desktop stays
  side-by-side with 20px to spare and is byte-identical throughout. Five widths were clean
  side-by-side before either query and stack now — 520, 528, 1224, 1232, 1240
  (423/431/**423.5/427.5/431.5**px of panel) — all just under the 432px threshold and all fully
  readable stacked. The last three were first recorded as 424/428/432 from `clientWidth` − 32,
  which rounds: 432 could not have been in a "stacks now" list, because at exactly 27rem the
  side-by-side arm applies. The panel is 431.5px there.
  Second pass, on the stacked arm only: the fields column carries `gap-2` below 14rem and keeps
  `gap-0.5` above it. Stacked, a label sits 0px from its own value, so 2px to the next field read
  as eight equal lines rather than four labelled pairs; measured at 768 the gap between fields is
  now 8px against 0 inside one, and at 360 and 1280 it is still 2px.

- ~~**`AlbumRow`'s subtitle truncates to zero width at 360px**~~ — **CLOSED 2026-09-08, same
  mechanism, and the stop condition held.** The row's text column declares `@container/rowtext`
  and the subtitle line stacks below 18rem of column, dropping the separator with it. The
  threshold is one number for callers with different meta widths: in the row arm the meta slot is
  max-content —
  130.5px in the import feed, 185.1px on `/duplicates` — so one line needs 218px/272px for a 66px
  artist, and at 288px they get 136px/82px. Measured on all three surfaces at 320→1920 in steps of
  32: **before**, the subtitle was `clientWidth` 0 at 320 and 360 on all three surfaces and cut at
  414 (49/66px feed and Review, 18/66px duplicates); document overflow was 19px on `/duplicates`
  at 320.
  **After**, the subtitle is the full column width at every width, the middot no longer opens the
  line, and document overflow is 0. **136 of the 153 surface×width measurements are byte-identical
  to before; the 17 that changed are all at viewports ≤480.** Four of them (the feed and Review
  rows at 448 and 480) had a whole subtitle already and now stack — the price of one threshold
  sized for the wider `/duplicates` meta. Adjacent, NOT fixed: the TITLE row has the same shape
  (`min-w-0 truncate` title beside a `shrink-0` badge) and is cut at 320 — 7/38px in the feed,
  2/87px on `/duplicates`, whole from 414. Whether the title or the badge wins that space is the
  same `AlbumRow` design call, and it is the owner's.
  **Scope of the three surfaces above: there are FIVE `<AlbumRow>` call sites, and the sweep
  measured three.** `ReviewPage`'s inbox row passes a fixed `N tracks`, so the threshold holds
  there by the same arithmetic. `BankSection`'s did not, and that is fixed separately below.

- ~~**The keeper radio on `/duplicates` sat below the row it selects**~~ — **CLOSED 2026-09-11,
  the same drift as the bank row's checkbox, on the other page that has a selection control.**
  `MemberRow`'s `<li>` was `flex items-center` and held the `AlbumRow` **plus** the folder-path
  scroller, so the radio centred on both: swept 320→1920 in steps of 8 with a real 15px scrollbar,
  it sat **17px** below its own row at 320→768 and **12px** at 776→1920 — a nonzero drift at all
  201 widths, never 0. (The 17/12 split is the path scroller's own horizontal scrollbar, which
  only appears while the path overflows.)
  Fixed with a two-track grid rather than the bank row's stacked flex wrappers, because the inset
  the path line has to clear here is the NATIVE radio's width — 13×13 in Chromium, the UA's number
  and not ours. The path goes in row 2 of the radio's own column track, so the offset is derived;
  restating it as a padding is off by 1px (`pl-10` = 40 against a measured 41), and it would be a
  different number in another engine. **After: drift 0 at all 201 widths**, the path text starts
  at exactly the cover's left edge at all 201 widths (82px ≤414, 312px ≥768, identical to before),
  and the `AlbumRow` column geometry and every `<li>` height are byte-identical to before at all
  201 widths — the only thing that moved is the radio.
  **Swept for the shape (statically, not measured): these two were the only instances of it.**
  The app has seven other selection-control call sites. Two cannot grow a second line at all
  (`BrowsePage`'s facet rows and `ImportPlaylistsPage`'s Plex rows both `truncate` their label).
  Four can — `LyricsBackfillPanel.tsx:86`, `MergePlaylistDialog.tsx:144`,
  `PlaylistDetailPage.tsx:582`, `ReleaseSearchRow.tsx:98` — but each is a control beside its OWN
  wrapping label, where centring on the label is the conventional treatment, not a control
  detaching from a separate row it selects. `BankSection.tsx:291` is a header checkbox with a
  two-word label. None was browser-measured.

- ~~**A failed bank row's error overran the row and starved its subtitle**~~ — **CLOSED
  2026-09-08.** `BankSection` joined `row.error` into `AlbumRow`'s `meta`; that slot is
  `shrink-0`, so in the ROW arm its used width is max-content and it can neither shrink nor wrap,
  and the string is `str(exc)` from `app/bank/apply_runner.py` — unbounded. There are TWO such
  sources: `:281` re-raises the runner's exception, and `:506` writes
  `state.error or "import failed"`.
  **Every width below is measured with a real 15px scrollbar present**, 320→1920 in steps of 8
  (201 widths). The first pass hid it and its numbers are corrected here; the tell was an exact
  15px delta on every viewport-derived figure.
  Measured on `/review` with a matched-but-failed row (76% · medium · a 115-character beets
  error): the meta cell went **845px** wide — a max-content width, so it depends on the string,
  not on the scrollbar — and the sibling `min-w-0 truncate` subtitle was `clientWidth` **0 at 89
  of the 201 widths, 584→1336**, with a gap at 768–808 where the `md` sidebar drops the column
  under 18rem and the line stacks. (Any width COUNT here is a function of the error string's
  width; re-deriving it with a different 115-character string moves it. The two endpoints
  recorded before — 568→1280 in a code comment, 568→1368 here — were both wrong AND
  under-determined.) The meta's ink spilled past its own column at those same 89 widths.
  No cap fixes it in the slot: the widest legitimate meta across the other four callers is 185.1px
  (`/duplicates`) and the 18rem threshold is sized for that, so keeping the budget leaves the error
  about 9 characters. Like-for-like against that ceiling the error cell was **4.6×** it.
  The percentage and recommendation stay in the slot; the error moved to its
  own line below the row (`break-words`, `line-clamp-2`, whole string reachable through the row's
  own Open link). **After**: the subtitle is 0-width at no width, the meta is 130.5px (the
  humanized `Medium match`, up from 88px for the raw enum), its ink never leaves the column, and
  document overflow was 0 before and after. A failed row is 32px taller with a one-line error and
  52px with a two-line one, at 1280.
  Second pass fixed two things this one shipped or left. (1) The error line went inside the `<li>`'s
  `items-center` body, so the select checkbox centred on row+error and drifted **16px** below its
  own row on a one-line error and **26px** on a two-line one, measured at 1280; the control and
  the row now share their own `items-center` wrapper and the error is a sibling below, and the
  drift is **0/0/0**. (2) The meta's ink still ran past the column at **3 widths** (320/328/336,
  22.81px at 320) on an ordinary row, and at **8 widths** (320→376) on a `needs_review` row, where
  it painted INSIDE the Ignore button's hit rectangle — hit-tested with `elementFromPoint`, a tap
  on that text fired Ignore. The two counts are one sweep over two row kinds, not a contradiction:
  a `needs_review` row carries a fourth control, which narrows its text column (0px at 320 against
  28.19px without it), so its ink escapes at more widths. `AlbumRow`'s subtitle/meta line now
  carries `overflow-hidden`: **0 control hits at all 201 widths**, against those 8 widths and 100
  sampled ink columns with the class removed.
  The text column itself is **28.19px at 320 and 68.19px at 360** (43/83 was the scrollbar-hidden
  reading).
  Adjacent, NOT fixed: at 320 a bank row's title renders at `clientWidth` **0** — it does not
  ellipse, it vanishes, and no "…" fits. At 360 a row without the Ignore button gets 10px (the
  ellipsis glyph, no character); a `needs_review` row, which carries that extra button, is still
  0 at 414. Its text column is 0px there, so the subtitle and meta are clipped away with it. That
  is the same `AlbumRow` title-vs-badge density call recorded above, and it is the owner's. The
  badge is what takes the space: it is `shrink-0`, and at 320 its ink is the only text left in the
  row — it extends ~106px past a 0-width column.
  **It does not reach `Open`** (re-measured 2026-09-11, correcting the sentence that stood here).
  The badge box ends at **239.8** and `Open` starts at **248.97** — 9.17px clear, **0px of
  overlap** — and the two do not even share a vertical band (badge 456→478, the action controls
  491→523). `elementFromPoint` swept across the badge's whole painted ink band returns the badge
  and nothing else, on every row and every width sampled. So the "covers 25px of the 56.8px
  `Open`, inert (a tap there hits the badge)" sentence recorded here was wrong in both halves: the
  25px is real but it is not the badge, and there is no overlap for a tap to be inert against.
  What the 25px actually is, is below.
  **The "Adjacent, NOT fixed" title-at-0px material above is CLOSED 2026-09-11** — on
  `fix/phone-width-rows-and-hit-areas` (PR #221, squash `a053ffc` = v0.51.2), vault decisions 39:
  below 28rem of ROW the bank row's action group takes its own line under the row, so the text
  column keeps the ~169px the group was taking. Re-measured over the same 201 widths with the
  scrollbar present: the title is **23.02px at 320** (0 before) and never 0 at any width, on a
  `needs_review` row carrying the widest badge the app can render ("Already in library",
  107.98px) and its fourth control. Measured on TWO of `AlbumRow`'s FIVE call sites — the bank
  row (`BankSection.tsx`) and the decision row (`ReviewPage.tsx`) — each with its own measured
  threshold. The other three, corrected 2026-09-11 — the sentence here read "`/duplicates`'
  member row has no action slot", which does not reach, because an action slot is not what
  takes the space:
  * the **inbox row** does not reproduce it (title never below 37.8px, one control, no badge)
    and is untouched;
  * **`/import`'s feed row** DOES reproduce it, and is now FIXED under its own struck entry
    (the owner extended decisions 39 to it on 2026-09-11);
  * ~~**`/duplicates`' suggested-keeper member row reproduces it**~~ — **CLOSED 2026-09-11**,
    the owner's ruling on the same branch (decisions 42): *"Move the badge to the meta line"*. It has no
    action slot; the `shrink-0` "most complete" badge on the TITLE line was what starved the
    title — `clientWidth` **0 at viewport 320 and 328** (row 223/231), **3.39 at 336**, first
    clearing the 11.33px glyph at row 247 (viewport 344), while the badge-less sibling showed
    114px at 320. With the badge on the meta line, re-swept 320→1920 in steps of 8 with a real
    15px scrollbar: the keeper's title is **114px at 320** and equals the sibling's **to the
    pixel at all 205 widths** (max difference 0), and **no title is at or below the glyph at
    any width** (four were before). The badge is never cut — 0 of 205 widths, counting both an
    ancestor clip and Badge's own `overflow-hidden` — because below 28rem of the text column it
    stacks under the meta text instead of beside it, and at viewport 320, where the column is
    114px against a 114.61px badge, `whitespace-normal` lets the label wrap (badge 114×38
    there, 114.61×22 at the other 204). Row widths are identical to before at every width, the
    radios' accessible names were unchanged by the move (the keeper's has since gained its
    badge text — below), no control is clipped and no text ink answers
    `elementFromPoint` with a control. The badge also lost 4px (118.61 → 114.61): the icon's
    `mr-1` restated a `gap-1` Badge already applies.
    **The cost is vertical, and it is the keeper row only:** that row grows **26px** at the 56 widths
    where the badge stacks (viewport 328→648, and 768→880 where the sidebar step narrows the text
    column under 28rem again), **42px at 320** where the label wraps to two lines, and **2px** at the
    144 widths where it is inline (656→760 and 888 up; re-read by the UX seat after the move), its 22px
    box being taller than the 20px meta line. The sibling row and the card's primary button move down by the same amount —
    row-relative nothing else changes, and there is no horizontal overflow at any width
    (document `scrollWidth` equals `clientWidth`, 320 through 1920).
    **Two more from the same pass, recorded and not fixed (UX seat, final round of the branch):** at 35 of
    201 widths (viewport 496→648 and 768→880) the stacked badge sits under the META's start,
    105–208px in from the text column's left edge, because subtitle and meta go inline at 18rem
    of the column while the badge stacks until 28rem; flush-left would mean stacking on the meta
    LINE rather than inside the meta span, an `AlbumRow` change. And on `/review` at 392→512
    the two dropped arms do not share a left edge — the decision arm sits under the cover
    (x≈43 at 480), the bank arm past the checkbox column (x≈88) — each aligned to its own grid
    track, so neither is wrong, but they read as unrelated in one scroll.
    **And one closed after it, same branch:** "most complete" was in no radio's accessible name —
    a sibling text node, so a screen reader arrowing the group never heard which member the app
    recommends, on the screen that trashes the others. The keeper's name now ends in the badge's
    text (one spelling, a shared constant), which also makes it distinct from every sibling. Two
    NON-suggested members sharing a format and bitrate, or with neither to show, still read the
    same; the folder path is the only always-distinct field, and putting it in the name is a
    design call for the owner, not this fix.
  `AlbumRow` itself is unchanged.
  **What decisions 39 did NOT settle, recorded as the owner's call:** below the threshold the
  BADGE still owns the title's line. At viewport 320 a bank row's text column is **139px**
  (255 of row, less the 32px checkbox slot, the 16px padding either side, the 40px cover and
  its 12px gap) and the badge takes **107.98** of it plus an 8px gap, so the title renders
  **23.02px** — `Lift…`. Enormously better than the 0 it replaced, and on `/import`'s feed
  row, which has no checkbox slot, it is 55px. But if the title is to be READABLE on a phone,
  the lever is the badge, not the action: moving it to the meta line returns ~110px.
  **The owner took that lever on `/duplicates` on 2026-09-11** (*"Move the badge to the meta
  line"*) — measured in the closure above: the keeper's title goes 0 → 114px at viewport 320
  and matches a badge-less sibling at every width. It is NOT taken on the bank row or the feed
  row, whose STATUS badges still own the title line below the threshold; that is still a design
  call and still the owner's. Re-read by the UX seat on the
  branch's final round: 1–3 characters of title on the bank rows at viewport 320–352 and
  513–543 (23–24px at 320), eight widths under 60px — the loudest remaining instance of the
  defect decisions 42 settled on `/duplicates`.

- ~~**A bank row's `Open` button is SLICED by the list's own `overflow-hidden`**~~ — **CLOSED
  2026-09-11** (on `fix/phone-width-rows-and-hit-areas`, PR #221, squash `a053ffc` = v0.51.2; vault
  decisions 39): the button is on its own line under the row below 28rem, where the slice
  happened, so the list's `overflow-hidden` has nothing to cut. Measured over the same 201
  widths: **0 controls clipped at any width** on all five bank rows, against 24.78px of the
  56.81px `Open` at 320 (16.78/8.78/0.78 at 328/336/344). Below is the original entry.

  **A bank row's `Open` button is SLICED by the list's own `overflow-hidden`** — pre-existing,
  unchanged by this branch, NOT fixed (measured 2026-09-11). On a `needs_review` row, whose action
  slot holds Ignore + Remove + Open, the button is **56.81px** wide and the `<ul>`'s
  `overflow-hidden rounded-xl border` cuts **24.78px of it at 320** — 16.78 at 328, 8.78 at 336,
  0.78 at 344, 0 from 352: **4 widths**. That is the 25px the entry above misattributed to the
  badge. A clipped control is worse than an overlapped one: the missing 44% is not in the hit
  rectangle either, so the row's primary action is part-invisible AND part-untappable there.
  Identical on the merge-base (`2950304`) and on HEAD, so nothing on this branch caused or changed
  it. Fixing it is the same density call as above — what wins the row at 320 — and it is the
  owner's.

- ~~**A BANK ROW's stacked meta line is cut mid-word with no ellipsis**~~ — **CLOSED
  2026-09-11** (on `fix/phone-width-rows-and-hit-areas`, PR #221, squash `a053ffc` = v0.51.2; vault
  decisions 39), and NOT by the rejected `truncate`, which is still rejected for the reason
  below. **The title is scoped to the bank row deliberately** (2026-09-11: it read
  "`AlbumRow`'s stacked meta line", a component-wide claim its own body then contradicted).
  The cut was the column being too narrow for the word, and the dropped action line gives that
  column the group's width back: re-measured over the same 201 widths, **0 widths cut on every
  bank row**, against 13 widths and up to 51.86px hidden. The meta TEXT is unchanged (the keeper's meta span on
  `/duplicates` now also holds the badge — closure above) — `AlbumRow` was not touched, so `/duplicates` keeps its format and bitrate at every width.
  **The other four call sites cut 0 widths, re-measured after `/import`'s row was fixed too**:
  swept 320→1920 in steps of 8 with a real 15px scrollbar over `/review` (9 rows), `/duplicates`
  (2) and `/import` (6, every status), every row that renders a meta span has **0 px** of that
  span's box clipped at every width. The span is selected structurally and checked (it is the
  `shrink-0` last child of the meta line, beside the `truncate` subtitle) — picking "the last
  span" scores a normal subtitle ellipsis as a mid-word cut, and picking
  `.overflow-hidden` finds the BADGE, which carries that class and comes first in document
  order, and then reports every row as clean.
  Below is the original entry, kept for the rejected fix's measurements.

  **`AlbumRow`'s stacked meta line is cut mid-word with no ellipsis** — NOT fixed, and the
  candidate fix was built, measured and rejected (2026-09-11). The `overflow-hidden` added above
  sits on the subtitle/meta WRAPPER while `truncate` is on the sibling subtitle, so it never
  reaches the meta span. Stacked, that span is column-width and wraps, and any word wider than the
  column is cut at the box edge: **13 widths (320→416)**, up to **58.08px** hidden on a
  `needs_review` bank row (0px column at 320) and 20.03px on an ordinary one (28.19px column) —
  it renders as `91%` / `· Stro` / `matc`. `/duplicates` and the import feed cut 0 widths.
  The contained fix is `min-w-0 truncate` on the meta span with its `flex`/`shrink-0`/
  `items-center` moved behind the existing `@min-[18rem]/rowtext` query. Built and swept, 201
  widths, real scrollbar: it does what it promises — the row arm is byte-identical on both pages
  at every sample, and on `/review` the cut becomes a real `…` and the row is 20–40px shorter.
  **It was not landed because `truncate` also stops the line WRAPPING.** On `/review` that ellipses
  18 further widths (344→488) where the wrapped line fitted whole. On `/duplicates`, where HEAD
  hides nothing at any width, it hides **7→79px at 10 widths (320→392)** — and what it hides is the
  format and bitrate: at 320 two copies both read `2001 · 11 tracks · …` and become
  indistinguishable on the one page whose purpose is telling them apart.
  So the choice is a mid-word slice on bank rows against losing the compare data on `/duplicates`.
  That is the same "what wins the narrow column" call as the two entries above, and it is the
  owner's. Note the present state is still strictly better than what it replaced: the same ink used
  to paint over `Ignore`/`Remove` and take the tap.

- ~~**The same unbounded error pushed the WHOLE PAGE sideways on the bank detail screen**~~ —
  **CLOSED 2026-09-08.** `FailedBanner` rendered `item.error` — the same `str(exc)` the row above
  moved out of its meta slot — with no wrap, inside `StatusBanner`'s `min-w-0 flex-1` column,
  beside a non-shrinking "Open Duplicates" action. Measured at 360 with a 131-character unbroken
  path (scrollbar present): the ink ran **589px past its column** and **436px past the banner's
  own right edge**, and `documentElement.scrollWidth` was **757 against a clientWidth of 345** —
  412px of horizontal page scroll, not just a spilled column. At 768: 411px past the column,
  987 against 753.
  Fix is `break-words` on the `<p>` and nothing else. NOT a clamp: this page is the diagnosis
  surface and has to show the whole string. `overflow-wrap` lowers no ancestor's min-content
  floor, and the `<p>` is a block child of the `min-w-0` column rather than a flex item, so it
  needs no second `min-w-0` (the row's version does — there the text IS a flex item). **After**:
  the ink stays inside the column at both widths, `scrollWidth == clientWidth`, and the string
  takes 11 lines at 360 and 4 at 768, entirely inside the banner box. The 360 column is 167px of
  the banner's 321 because the action slot is `shrink-0`; that is StatusBanner's shape, unchanged.
  Also guarded: a whitespace-only `str(exc)` now renders nothing instead of an empty line.

- **The `view` link in `CandidateReview`'s match header has no `focus-ring` class** and falls back
  to the UA outline, while `styles.css:191-208` calls `focus-ring` this app's one dialect. Belongs
  with the pending global focus-ring decision, which is the owner's.

## Accepted residuals and deliberate decisions (not work)

Nothing in this section is a task. Each item was decided, with its reasoning, and is kept
because a recorded decision is what stops the question being reopened from scratch. Do not
scan here for something to pick up — scan *Open bugs / hardening*. Revisit an item only if
the condition it names has changed.

- **The source-missing refusal asks "is there nothing to import", not "is every source there"**
  (2026-09-20, `feat/import-keep-downloads`). A start whose sources are all absent is refused with
  `That folder doesn't exist.` before any job exists; one absent member of a list is NOT refused.
  Decided, with the reasoning, because the narrow predicate looks like an oversight:
  * **A stat cannot close the window it appears to close.** The inbox route re-derives its folder
    list server-side milliseconds before the start, and a folder is as free to vanish after the
    stat as before it. A guard placed over a race it cannot win is worse than none, because the
    next reader trusts the path afterwards.
  * **beets already answers the one-member case.** A missing toppath takes the single-FILE branch,
    `read_item` returns `None`, that toppath contributes nothing and the rest import.
    `test_review_all_survives_a_folder_that_vanished_since_the_listing` pinned this BEFORE the
    refusal existed, in as many words, and still does.
  * **The check is existence, never `is_dir`.** beets imports a single file as one track; an
    `is_dir` guard would take away something the engine can do. Pinned by
    `test_a_source_that_is_a_FILE_is_not_refused_by_the_existence_guard`.
  * **Absent and unreadable are told apart.** `os.path.exists` answers False for `EACCES` exactly
    as for absent, which would have reported the commonest self-hosted misconfiguration (a PUID/GID
    mismatch on a mounted share) as a typo. The check is one `os.stat` per path with an errno
    split: `ENOENT`/`ENOTDIR`/`ENAMETOOLONG` keep `That folder doesn't exist.`, anything else says
    `That folder can't be read. {strerror}.` — `strerror` is the OS's own summary and carries no
    path, the same reasoning `app/beets/library.py` already relies on. The classification rides on
    the exception (`unreadable: bool`) so the batch route's plural copy cannot re-bury it.
  * **`startImport`'s own 422 branch stays asymmetric with the shared helper, deliberately.**
    `throwIfRefused` shows only our own STRING detail, so FastAPI's array-shaped validation 422
    stays machine copy; `useImport.ts`'s `startImport` still reads both shapes, which its docstring
    records as carry-forward. The one body-validation 422 `POST /api/import` can send in practice is
    the 4096-character path cap, where the validator's own message is MORE useful than the page's
    generic sentence. Revisit if a second body rule lands.
  * **The batch sentence is plural even when the batch held one folder.** `settled_folders` can
    legitimately return a single folder, and if it vanishes the user reads "Those folders are no
    longer there." about one. Accepted rather than made number-aware: the route is a batch route,
    the browser is never shown which folders it handed over, and a count-dependent ternary would
    put two spellings of one refusal in the code to fix a sentence that is not wrong, only loose.
  * **It is a guard, not the cure.** The typo that prompted it came from a free-text path field.
    The folder browser in *Next up* removes the typo at its source; this refusal is what stands in
    until then.

- **"Stop this run" — what it deliberately does not do** (2026-09-19, `feat/import-keep-downloads`,
  replacing the run page's "Start over"). Stop is beets' own `ImportAbortError` raised at the next
  session hook, so the album it lands on is asked again when the folder is added again; what
  already landed stays. "Asked again" holds under the two settings the app writes (`move`,
  `hardlink`, vault `decisions` #53); a user-written `copy:`/`link:`/`reflink:` keeps no beets
  history, so a re-add offers the landed albums again as duplicate questions, never as a second
  import. Decided, not bugs:
  * **Offered on manual runs only.** The API accepts any origin, but an inbox or bank drain starts
    the next queued item as soon as the stopped one ends, so a Stop there would read as "nothing
    happened". The page shows the control for `origin === "manual"`.
  * **"Stopping…" has no bound.** A lookup already in flight finishes first, and an "as tracks"
    expansion in flight is one abort point for the whole album (every remaining track is looked up
    and resolved), because the alternative splits one album across two locations. Each remaining
    track is a MusicBrainz lookup of its own (beets' singleton pipeline calls `tag_item` per
    track), so on a long album that "Stopping…" is minutes, not seconds; not timed.
  * **A stop accepted after the last abort point ends the run whole, and the page says so.**
    `stopped` says a stop was accepted; `aborted` (on the contract since the pre-push round) says the
    stop reached the worker and ended the run early. Every verdict (ledger outcome, bank row status,
    applied count) reads the landed evidence first, and the done panel keys its stop-specific copy
    ("Import stopped", the rest stayed in the folder, the Add-from-folder CTA) on `aborted`, so a
    stop that landed after the last album had been placed renders as the finished run it was, with
    one muted line for the presser: "Nothing was left to stop." (manual runs only; one predicate
    and two assertions to remove if the owner prefers silence). The
    one window left: a stop accepted after the last hook on a run that then genuinely imports
    nothing names the stop, not the lookup, in the bank row's error.
  * **One merged album counts as two applied** — the merge row and the merged task's row are both
    counted, pinned by `test_a_merge_that_landed_before_the_stop_still_counts_imported`. Pre-branch
    behaviour; whoever revisits the merge exemption moves the count with it.
  * **The Stop concept is `StopCircle` at the app's one icon weight** — owner's call 2026-09-20,
    replacing the filled `Stop` square this entry used to record. It went through both alternatives
    in one evening, and the order matters: the fill came off first (a plain re-export, so
    `ICON_WEIGHT` reaches it through `IconContext` like every other concept), then the owner saw the
    result RENDERED in Orca at 40 px and switched the glyph to the ringed one. The two seats that
    originally called the light square "an empty checkbox" were right about the shape even though
    the token measurements said the confusion was unlikely — `--muted-foreground` at 7.63:1 against
    a real checkbox border's `--input` at 1.47:1, and `/import` renders no `Checkbox` at all (they
    live on `/import/albums/:index`, which never co-renders). **A measurement that says "unlikely to
    be confused" is not the same as looking at it.** The ring carries the "control" meaning the fill
    used to, without leaving the single weight, and the sweep's paused panel — the same `EmptyState`
    with a light `Pause` — still agrees with it. **Do not re-add a weight wrapper** —
    `icons.test.ts` pins glyph and weight together, in both directions.
  * **Smaller, left as read by the UI seat:** at 360 the error line above the button indents it
    by the pair's right alignment; the pending label ("Stopping…") shrinks the button and shifts its
    glyph, as the sweep's "Pausing…" already does; the done panel's two CTAs point at two homes
    (`/import` and `/review`), now at one weight; "run" is the owner's word.

- **The three app-owned writers are deliberately NOT descriptor-anchored** (2026-09-12,
  `fix/descriptor-anchored-library-writes`). `config.yaml`, the two artist-image toggle files and
  playlist artwork are written under the beets data dir and the playlists store, not below the
  music root, so owner ruling #45 (refuse a symlinked component below the library root) does not
  reach them and there is no root for `open_below` to descend from: a symlinked component of the
  beets or data directory is still followed, exactly as before. What they did get is the shared
  sink's unpredictable `O_EXCL|O_NOFOLLOW` temp. Nothing pins the placement — a future caller
  that put one of these files inside the library would get the unanchored write with no failing
  test.

- **Old-shape and artwork-cache temp leftovers are never swept** (2026-09-12, same branch). The
  sweep is bounded to this writer's own shape (`.<pid>.<16 hex><suffix>.tmp`); a leftover
  `.<name>.<pid>.<hex>.tmp` from the previous recipe and `app/artwork/cache.py`'s own
  name-embedding temps do not match it, deliberately — the regex cannot tell them from a target,
  and a bare `.*.tmp` glob would unlink files this app never wrote. They stay until someone
  removes them by hand. `artwork/cache.py` keeps its own writer on purpose.

- ~~**`os.fsync` of a directory has no behavioural pin**~~ — **CLOSED 2026-09-12** by the same
  branch's second fix round, as a side effect of the ENOTSUP swallow: both directory fsyncs go
  through `fsutil.fsync_dir`, and the test that a patched `os.fsync` answering **EIO** still raises
  fails if the call is deleted. What is still unpinned is DURABILITY — a lost directory entry needs
  a crash harness to observe — so the pin says the syscall happens, not that it works. (The
  cross-device copy's missing container fsync, first recorded here, was added in the first fix
  round: `_publish_then_unlink` fsyncs the container's descriptor before the source is unlinked,
  pinned by the order of the two syscalls' inodes.)

- **A row whose folder is outside the library root BY SPELLING is now refused, for art and for
  lyric sidecars** (2026-09-12, same branch — a behaviour change, accepted). `Path.relative_to` is
  spelling-based and `_music_dir` normalises without resolving links, so a legacy row imported as
  `/mnt/music/…` while `directory:` reads `/music/…` gets `status="failed"` / no sidecar and one
  WARNING where it used to be written. `get_artist_dirs` compares by spelling too — it reads
  `album.item_dir()` against the root, and the fix round also skips an album whose item dir IS the
  root, so a flat `path_formats` library answers `no_folder` instead of `failed` (measured: before
  the branch that layout wrote the poster ABOVE the root). It does not require containment: the
  writers' refusal answers that. The remedy for a genuinely mis-spelled library is to fix
  `directory:` or re-import, not to loosen the check.

- **The cross-device copy's final unlink is by NAME, and cannot be otherwise** (2026-09-12, same
  branch; security seat L-1). There is no unlink-by-fd, so `_publish_then_unlink` re-lstat's the
  source through its descriptor and skips the unlink on an identity mismatch, with one WARNING: a
  swap inside those two syscalls leaves the copy in Trash and the newcomer on disk — never an
  unlink of a file that did not reach Trash. Measured before the narrowing, the newcomer was
  unlinked without reaching Trash. (The bare-name residual that used to sit here is closed: both
  seats measured an absolute name renaming the file onto itself and REPORTING SUCCESS, so
  `_refuse_a_non_bare_name` now runs ahead of the staging stats.)

- **CLOSED 2026-09-12 — the window between the Trash's creation and the stat that identified
  it** (same branch, closed by its third fix round). It was titled "two microseconds" and
  measured **18 µs** (median; p99 22), and the record named only the shape that fails safe. What
  it actually bought, measured by the security seat: a real racer won it **537 times in 100 876
  requests (0.53 %)**, and a won window put that request's files outside the music library and
  pointed `empty_all`'s `rmtree` at a directory of the attacker's choosing — an escalation from
  "write inside the music library" to "recursive delete anywhere the app user can write", for one
  request per win. A forced artist-art sweep asks once per artist, so ~5 expected wins per
  thousand artists. The DEFAULT `<beets_dir>/trash` layout was the no-gain case throughout (that
  chain is the operator's, and the layout rule keeps the beets dir out of the library).
  `_ensure_trash_root` now returns `fstat` on the descriptor its own anchored walk reached and
  `protected_trees` takes it as `trash_ident=`, so there is no second resolution by name to race:
  the mover's own open must land on the directory the walk reached or be refused
  (`tests/test_trash_root_creation.py::test_the_identity_the_movers_get_is_the_one_the_walk_opened`).
  One residual, accepted and unchanged: a real directory a stranger LEFT at the configured path is
  adopted, and no check here can undo that — a party who owns the Trash's location owns it
  whoever creates the directory. `checked_protected_trees` creates the Trash, so no mover creates
  it and none sees `trash=None`; the folder-movers entry above still wants the same
  descriptor-shaped fix on its own three paths.

- **The create loop never re-asks whether a component is below the music root, so a part that
  acquires the root's identity inside that window is not noticed** (2026-09-13, same branch;
  security seat L-2', re-measured by the code seat's Q1f and the security seat's Q5).
  `_open_the_trash_chain` (`app/beets/store_layout.py`) walks the EXISTING prefix asking
  `_below_the_music_root` per component, then carries that decision into the create loop —
  measured by tracing the question: the request that creates `srv`, `x` and `.trash` asks 9 times
  and stops at their parent, the next request (they exist) asks 12.
  **The reachable input** is a part that becomes the music root by IDENTITY between the
  `os.mkdir(part, dir_fd=fd)` and the `os.open(part, …, dir_fd=fd)`: the root renamed onto it,
  bind-mounted there, or its parent renamed into the library. A real directory a racer CREATES
  at that path is not one — its parent is the descriptor the walk stands on, so it is outside the
  library — and that half of the old wording is gone.
  **The measured consequence** (Q1f, with the window driven by wrapping `os.mkdir`): `below`
  stays false, so a link the attacker planted at a later component is resolved and followed, and
  the Trash landed at that link's target OUTSIDE the library with the library-presence guard
  skipped — not merely inside the library unanchored. A symlink left in the window is still
  refused when its target reaches into the library, because resolving it is the step itself
  rather than a question asked about it.
  The other seat measured across five other shapes and agrees it is not worth a flip:
  re-asking changed no outcome (a link is decided by `_follow` either way, a `rename` moves the
  directory OUT of the library, and a bind mount defeats the `..` climb regardless), and the
  mutant that always asks passes the whole suite — no test pins `ask=not create`.
  `before_creating` (the library-presence guard) is still decided before the loop runs.
  Precondition unchanged: write access on the OPERATOR's chain above the music root, which is
  outside the threat model the layout rule is written for (the attacker's reach is inside the
  library) — the reason this is a residual and not the bug above it. Not driven as a race, only
  measured as the asymmetry.

- **The Trash listing's non-regular-file gate is a stat before `Item.from_path`'s own open, so
  the STATIC plant is closed and the race is not** (2026-09-13, on the link-target branch; code
  seat W2). `trash_manage._is_a_regular_file` stats the name and `Item.from_path` then
  re-opens it BY NAME, so a planter who swaps a regular file for a FIFO between the two still
  parks one `run_in_threadpool` worker for the life of the process: measured with the window
  driven in the gate's own stat, the listing thread was still blocked after 5.0 s. The Trash root
  is attacker-writable under the layout rule's own model, which is what makes the window
  reachable rather than theoretical; the precondition is winning it, and it was driven by
  injection rather than by two competing processes, so nothing here measures a win rate.
  **The record reader's copy of this BLOCKED-OPEN window is CLOSED as of 2026-09-14** (fix round 5,
  same branch; security seat H-1). `trash_origins._record_text` had the same stat-then-open shape
  and reached it from `GET /api/trash` and from every delete; it now opens the key ONCE with
  `O_RDONLY|O_NONBLOCK` and asks `fstat`, the size pre-filter and the bounded read of that
  descriptor, so the inode checked is the inode read. `O_NONBLOCK` closes an OPEN parked by a FIFO,
  a device or a lease; open(2) does not apply it to a read from a regular file, so a read on stuck
  storage still waits. Round 6 put a `stat` in front that refuses a non-regular name unopened and
  requires the descriptor to be the inode it saw; the DEVICE-OPEN window that leaves (a device
  opened after that `stat`) is a different one, recorded under round 6 above.
  What forced it was not the race but a write lease (`fcntl F_SETLEASE F_WRLCK`) on an ordinary
  73-byte record, which passes every content gate and blocks any other `open` for
  `lease-break-time` — 45 s, renewable, no race to win, and 45·K s of `beets_swap_lock` for K
  leased keys inside one `empty_all`. The FIFO half of it was measured at 0.90 % of calls at a
  10 % duty cycle. The LISTING's window (this entry) still stands, because its re-opener is
  `Item.from_path` BY NAME and closing it means reading tags through a file object.
  **The fd-shaped option: TAKEN for the record reader (2026-09-14), not for the listing.**
  `os.open(path, O_RDONLY|O_NONBLOCK)` returns in 0.0000 s on a FIFO (measured) and `fstat` then
  answers about the inode already held, which is what `trash_origins._record_text` now does. Here
  it is not that cheap: `Item.from_path` takes a PATH, so the same shape means reading tags
  through `mediafile.MediaFile(<file object>)`, supported in the pinned 0.17. That is a change to
  how every Trash row's tags are read, not a review-round edit, so it stays the owner's call.
  **The same window exists one step later**, on a different
  directory on each of the two arms — and only the move-back arm's re-opener is the app's own
  `_holds_media` walk (its sole call site is inside `_restore_to_origin`). On the IMPORT arm —
  every row with no usable record — the re-opener is BEETS, and the folder it opens is still in
  TRASH, the attacker-writable side under this model and therefore the cheaper half
  (0.690 ms between the pre-flight's return and the first `Item.from_path`, 12 files, measured
  2026-09-13, security seat L-3), while on the move-back arm it is in the music library one
  rename later. The note here named only the second. A third, strictly harder variant rides
  along: a directory removed mid-walk whose inode is reused by a sibling prunes that sibling from
  the pre-flight's `seen` set, and beets then opens it — it needs an `rmdir`/`mkdir` win inside
  the walk, and the exposure it buys is the same one this window already records.

- **The Settings report is silent for "music root absent + Trash spelled below it" while every
  destructive request answers 503** (2026-09-13, same branch; code seat W1, security seat L-4').
  Deliberate: the row used to fire for a `directory:` that is simply not there yet, which blocked
  Save for an edit that named no path (`test_validate_paints_no_row_for_a_directory_that_is_not_there_yet`
  pins the silence; `test_an_unmounted_music_root_is_reported_as_the_music_roots_fault` pins the
  refusal). Cost: a dropped share gives the operator no pre-delete signal, and the first thing
  they see is a 503 on their next delete. An ADVISORY row — not a blocking one, which is what
  made Save unsavable — is the shape not taken; it was out of the fix round's scope.

- **Changing beets' `directory:` leaves absolute paths in Trash origin records** (2026-09-14).
  A record stores the folder's absolute path at trash time (`trash._record_origin`), and
  `trash_origins.move_back_target` tests containment lexically against the current music root, so
  after a `directory:` move those rows lose their move-back and restore by re-import, with the
  "not inside the current music library" note. Library item paths follow `directory:` (beets
  stores them relative). The slskd inbox ledger (`acquisition/ledger.py`) also keys on absolute
  paths, but on the INBOX folder's path: `seen` matches `str(folder)`, so it follows
  `MUSICDROP_INBOX_DIR`, not `directory:`, and changing that mount makes earlier drops read as
  unhandled (code read).

- **The artist-image reset answers 409 while the beets swap lock is held, rather than waiting
  for it** (2026-09-12, `fix/reset-to-auto-confirms-and-moved-aside-trash-rows`). The reset is
  a Trash mutator, so it takes the same lock the three routes in `api/trash.py` take — and
  refuses the same way, with their sentence, via the LOCK half of the gate only (an import
  reads the lock and never holds it, so the job union would refuse a portrait reset for the
  length of one). Waiting was the alternative and was rejected: no holder is bounded (a
  restore re-imports, a duplicates merge runs a whole batch) and `apiFetch` sets no timeout,
  so the confirm dialog would sit pinned with Cancel disabled on an unbounded wait, with
  nothing on screen saying why. The gate is asked again with the lock held, because the
  pre-check's own window plus the acquire can outlive the flag it read. Revisit only if the
  reset gains a progress surface that can honestly show a wait.

- **The background portrait filler writes the auto slot with no gate, so a reset's auto clear
  can be undone by a filler write that starts after the inner gate read** (2026-09-12,
  `fix/reset-to-auto-confirms-and-moved-aside-trash-rows`). The inner re-check covers the
  sweep, not the filler's single-flight kick. The loser is one automatic image that the next
  lookup replaces; never a user upload, which the reset moves to Trash and the clear no longer
  touches. Recorded, not fixed: gating the filler would serialise a background fetch behind a
  user action for nothing a user can see.

- **An upload that lands while the reset is moving the override is not serialised against it,
  and the bound is "never lost"** (2026-09-12, `fix/reset-to-auto-confirms-and-moved-aside-trash-rows`).
  The swap lock covers Trash writers; the override upload routes take the art-sweep gate only.
  The reset moves the exact paths `override_files` returned and then clears only the auto slot,
  so a new upload published mid-move ends either in the Trash entry (moved with the old pair) or
  in the cache dir without its mime sidecar (served under the generic type until the next upload
  overwrites it). No arm unlinks it. Two tabs or a scripted client to reach; recorded, not fixed.

- **The reset's move-failure WARNING names the Trash dir in both arms, and its 503 carries two
  audiences** (2026-09-12, same branch). Two notes on the same 503 path, both deliberate.
  (a) The log line interpolates `store.trash_dir` even when the fault is the origins store —
  the less specific of the two paths, but nothing is missing: `trash_origins._store_unusable`
  logs its own path at WARNING in the same `%r`+`display_path` shape, so the operator has
  both lines. (b) The status is either a path-free user sentence (`_MOVE_FAILED` plus the
  `OSError`'s `strerror`) or the layout error's operator sentence, which names absolute paths
  on purpose — the house posture, identical at `api/trash.py` and `api/reorganize.py`. Noted
  because a reader who greps only `_MOVE_FAILED` would conclude the route never puts a path on
  the wire.

- **A `by_hand` Trash row puts the app's artist-image cache dir on the wire as its `origin`**
  (2026-09-12, `fix/reset-to-auto-confirms-and-moved-aside-trash-rows`). `origin` previously
  only ever named a path inside the music library or a recorded folder; a portrait **Reset to
  auto** moved records `files[0].parent`, so the row reads "Was at
  `/data/cache/artist-images`". Deliberate, and the point of the field here: the row's note
  tells the user to copy the files back into the folder it was at, and `origin` is what says
  which folder. Session-gated like the rest of the listing, and a sibling of `trash_path`,
  which the same response body already carries. Revisit only if an unauthenticated reader of
  the listing ever exists.

- **The Trash store is checked per artist, not per file — the check-to-use window is one
  artist's write** (2026-09-11, `fix/art-apply-keeps-hand-placed-art`). The runner resolves
  the store right before each artist's `write_artist_art`; a Trash dir re-pointed under the
  library between that check and the move lands the moved files there — still a move, nothing
  deleted. A symlink that PREDATES the container claim is refused (a dangling one fails the
  `mkdir`, a live one makes the allocator pick the next name); one swapped in after the claim
  is the open window recorded under *Open bugs / hardening*. Same window every other Trash
  caller accepts (delete, duplicates); the only forced caller today is the per-artist Apply,
  seconds long. Strictly wider than "per artist": the store check reads
  `app.state.beets_library` fresh while the runner writes through the `lib` captured at start,
  and a config Apply can swap it mid-run — traced fail-closed (a music dir re-pointed inside
  Trash makes the check refuse; a torn-down handle raises out of `get_artist_dirs` into the
  job's failed state before any write). Re-derive if a forced multi-artist caller appears.

- **A bank row's meta line is bounded by the enum in practice, not by the contract
  (2026-09-11).** `recommendationLabel` (`BankSection.tsx:445`) maps the tier through
  `RECOMMENDATION_LABEL` and falls through to the raw value on a miss — deliberate, so an
  unknown tier reads as itself instead of `undefined`. But the bank contract types the field
  as a bare `string` (`BankItemSummary.recommendation?: string | null`) where the import feed
  carries the `Recommendation` enum, so what bounds that slot's width today is what the server
  happens to send, not the type. The slot is `AlbumRow`'s `shrink-0` `meta` — the same slot
  #220 moved an unbounded `str(exc)` out of. Nothing to do while the server sends the enum;
  the fix, if the contract ever loosens in practice, is to narrow the field in the schema
  rather than to cap the label.

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
  state to store, reconcile or sweep. Rotating the effective hash invalidates every
  session at once (the signing key is bound to the hash — 2026-08-30 audit fix M3), as
  does deleting `<beets_dir>/session-secret`. That holds for both sources: a new
  `MUSICDROP_PASSWORD_HASH` plus a restart re-mints nothing, so every browser signs in
  again; the change-password form (**Settings → Account**, writing
  `<beets_dir>/password-hash` — first-run password setup, 2026-09-03) re-mints only the
  caller's cookie, so every OTHER browser is signed out and the UI says so. Pinned as
  intended behaviour by `test_a_token_copied_before_logout_still_works`.

- **The session-secret create race is narrowed, not closed (2026-08-30, auth slice 1).**
  Two processes hitting first-run together could each mint a signing key; the loser
  re-reads after `write_atomic_bytes` and adopts the winner's, but both could re-read
  before the other's `os.replace`. Accepted because the image runs uvicorn single-worker
  by design — there is no second process in the shipped deployment. Documented at
  `app/auth/session.py::load_or_create_session_secret`.

- **The first-run SETUP race is accepted on a trusted LAN — and the window is not bounded to
  first boot (2026-09-03, first-run password setup; vault decisions 29; amended by the
  same-day security review).** Whoever reaches the sign-in screen while no password source
  exists sets the password — there is no setup token to present. The window opens at first
  boot and it re-opens whenever the `password-hash` file becomes absent: the source is resolved
  per request, so deleting the file re-opens setup on a running server with no restart and no
  prompt (measured by the review seat), and a bind mount that comes up empty or a restore that
  misses the file does the same at the next start. Accepted because the shipped deployment is
  a single account on a trusted LAN. The mitigations are the ones in place, not a token: the
  firewall/LAN the deployment assumes; the `MUSICDROP_PASSWORD_HASH` override, which hides the
  setup form regardless of the file and keeps the credential outside the data volume; and the
  log line setup writes when it stores a password (WARNING, naming the file — added by the
  same-day fix round on this branch, 2026-09-03), so a claim is visible in the container log.
  Within the one process an `asyncio.Lock` around check-then-write makes "first wins" true
  rather than probabilistic: a second setup POST answers **409** instead of overwriting, and
  the image runs uvicorn single-worker by design (the same fact the session-secret race above
  rests on). A setup token printed to the startup log and required by the form remains the
  hardening if the box is ever exposed beyond the LAN — noted, not built.

- **The password is the one env-vs-file store where the ENVIRONMENT wins, and it wins even
  when UNREADABLE (2026-09-03, vault decisions 29).** `plex.json` and `slskd.json` let the
  saved file beat its seed env var; `MUSICDROP_PASSWORD_HASH` does the opposite and hides
  both the setup form and the change-password form. Deliberate: it is the lockout-recovery
  lever, and a mangled value (the compose `$$` trap) falling through to the setup form would
  mint a second password that the corrected env var later shadows. No env→file migration
  either — the env var is a chosen posture. Recorded in README's override subsection; do not
  "fix" the asymmetry to match the integrations.

- **The `docker exec … rm` recovery path and root-owned files under `/data` — UNVERIFIED
  (2026-09-03).** Inferred from reading, not from running a container: the image declares no
  `USER` (`Dockerfile`, beside the accepted `docker:S6471` comment), so `docker exec` runs as
  root, and `entrypoint.sh` re-owns `/data` with `chown -R` on every start. The documented
  forgotten-password recovery is therefore `rm` AND restart: the `rm` itself creates nothing,
  and the restart's chown runs before the app writes a fresh `password-hash` as its own user.
  What was not measured: whether an exec session that WRITES under `/data` (an operator
  piping `hash_password` output into the path by hand) leaves a file the serving user cannot
  read until that chown, and what the status endpoint reports meanwhile. Treat as UNVERIFIED
  until someone runs it in a container; README's recipe already carries the restart.

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
  restores it in a `finally`; a concurrent config Apply replacing `beets.config`'s sources
  between the two would leave a stale `fetchart.auto` overlay for the process lifetime. The
  docstring at `app/beets/cover.py:128-131` names the hole, labels it a documented
  residual, and names the eventual fix (removing the persistent overlay). Genuinely
  narrow: single user, requires an Apply mid-fetch. Recorded so "documented" is true for
  someone who has not read that function.

- **A symlinked Trash row keeps Restore and its own Empty DISABLED — the carve-out from
  `decisions.md` 27, now settled** (owner, 2026-09-02: *"Keep them disabled"*, recorded as
  `decisions.md` 28 item 1). 27's amendment (*"Keep it enabled, warn clearly"*) reasons that
  disabling Restore *"would have deleted a working recovery path in the name of safety"*,
  which is true of an `"import"` row: its files ARE in Trash, so the re-import is a real
  recovery. A symlinked row's files never moved, so a restore would import whatever the link
  points at rather than undo anything — that is the owner's stated reason — and
  `trash_manage.resolve_trash_child` already answers 404 to the per-row Restore and the
  per-row Empty alike, so disabling both controls deletes nothing that worked.
  `DELETE /api/trash/all` removes the link, and only the link. Quote 27 accurately if this
  is ever revisited: the phrase "every row" is nowhere in it (grepped 2026-09-02), and its
  amendment names only `"move_back"` and `"import"` — a `"refused"` row is outside what 27
  decided rather than something it forbids. Full shape: the shipped "Record the origin path
  at trash time" entry under *Open bugs / hardening*.

- **A dangling link at an album's original path REFUSES the restore, and MusicDrop does not
  replace it** (owner, 2026-09-02: *"Keep refusing"*, recorded as `decisions.md` 28 item 2).
  Replacing a broken link automatically was offered and declined: a restore never removes
  anything sitting at the origin. The occupancy answer (`fsutil.occupied`) uses `exists`,
  which follows symlinks, so a dangling link reads as absent, the move is attempted, and
  `os.rename` answers ENOTDIR — one of `fsutil.DEST_OCCUPIED`, which `move_no_merge`
  normalises to `FileExistsError` and `_restore_to_origin` reports as `origin_occupied`.
  The files stay in Trash and nothing moves, so the residual is the wording rather than the
  data; it is stated in `trash_manage._restore_to_origin`'s docstring, which also says why
  widening `origin_occupied` to name the broken link is a contract change.

- ~~**`awaiting_decision` can read true while beets works, and then STICKS**~~ — **FIXED on
  `fix/import-feedback-residuals`** (2026-09-08; **PR #220, squash `b8fb9f4` = v0.51.1**). The flag no
  longer comes from a consumer-side set of indices: `ImportBridge.has_unanswered_park` answers
  it from the park itself — a registered reply slot with no answer delivered into it — and
  `ImportJob.parked_awaiting` is gone with its two adds and both discards. The push marks the
  slot in the same critical section as the put, so the reading falls with the answer rather
  than with the drain that follows it, and one expression covers both channels. The trigger was
  WIDER than recorded above: the worker emits its needs_review / needs_dup_resolution outcome
  BEFORE it parks, so the row a client answers already exists, and any choice landing between
  the slot's registration and the park's queueing was accepted against a live slot — the
  duplicate channel needs one submit, not two. Reproduced deterministically by gating the park
  channel between those two steps —
  `test_awaiting_decision_clears_when_a_choice_beats_its_park_onto_the_queue` and its duplicate
  twin, both of which fail on the parent commit.

- ~~**The plain-space middot survives outside the import page**~~ — **CLOSED 2026-09-08, and the
  recorded defect did not reproduce.** `SEGMENT_SEP` moved to `lib/format.ts` and both
  `ReviewPage`'s decision row and `CandidateReview`'s match header now use it, so the app has one
  dialect for the `%` · `match` line. But the reason recorded here was wrong: nothing wraps in
  `AlbumRow`'s `meta` slot, because that slot is `flex-shrink: 0` and in the ROW arm its used
  width is therefore
  max-content. Measured in Chromium at 360px with a 68-character artist: 130.5px wide, ONE client
  rect, one line — byte-identical to the import feed's row, which has always passed the constant
  into the same slot. The slot clips rather than wrapping — since 2026-09-08 at the
  subtitle/meta line itself, not only at the list. The
  only place the glyph pair is load-bearing outside the status lines is the match header, which
  genuinely wraps (below). Adjacent, NOT fixed: at 360px that same row truncates the artist to
  zero width and leaves `AlbumRow`'s own separator middot leading the line ("· 76% · Medium
  match"), and the meta box overruns the text column by 4.8px — which of the artist and the
  match wins that space is an `AlbumRow` design call with app-wide reach. Since 2026-09-08 the
  overrunning INK is clipped to the column (see the bank-row entry above); the space question
  is untouched.

- ~~**The import status line is the same recipe three times**~~ — **CLOSED 2026-09-08: two of the
  three were one recipe; the third is a different one.** The feed's count line and the sweep's
  folder line shared a byte-identical `<p>` class string and are now one `StatusLine`. Their
  `<svg>` class EXPRESSIONS differed — `cn("mt-0.5 size-4 shrink-0", working ? "animate-spin" :
  "invisible")` against the literal `"mt-0.5 size-4 shrink-0 animate-spin"` — and coincide only
  while the feed is working, which is why the extraction needed a `spinning` prop. The resume banner is deliberately NOT a
  caller: measured in Chromium it is six properties apart, not one (gap 12px vs 8px, icon 20px vs
  16px, top correction 0 vs 2px because its icon matches the 20px line box exactly, `font-medium`
  vs inherited, the muted colour on the icon rather than on the line, no `min-h-5`), and it owns
  the `id` the Start button's `aria-describedby` points at. Covering it takes a variant used once.
  All three satisfy the invariant that mattered — the spinner's box centre sits 0px from the first
  line box's centre, measured at 1280 and 360 — and the two sites now name each other in
  comments, so the third cannot be missed silently again.

- ~~**`CandidateReview`'s metadata line collapses at 360px**~~ — **CLOSED 2026-09-08.** The
  wrap/shrink decision was neither: the row is a sentence, so it stopped being a flex row. As
  `flex items-center gap-2` the browser had to fit every segment on one flex line and squeezed the
  widest — measured in Chromium at 360px: 3 line boxes, the "·" alone on the first, "Medium" and
  "match" split across the next two, and the %, the second separator and the `view` link parked on
  the middle one, with the source list ellipsed to "C…". It is now one text flow: 2 line boxes,
  every segment intact, the full source list visible, and the second line OPENS with a middot
  because every separator is `SEGMENT_SEP`. The source list also loses `truncate` with the flex
  row — on the screen where the release is being judged, wrapping the label and country beats
  ellipsing them. Adjacent, NOT fixed and pre-existing (present in the before shot): the candidate
  page overflows horizontally by 17px at 360px, clipping the AFTER-IMPORT panel's "changed" badge.

- **Wire-safety net coverage caveats** (by design, recorded so nobody assumes otherwise):
  SSE `/api/events` bypasses the response class (scopes are tag-derived today, never paths);
  any future route-level `response_class=` or hand-built `JSONResponse` bypasses both halves
  of the net.

## Open questions

- **What should a "Try again" button do while it retries, and where should focus go after?**
  (PR #232 round-13 UI seat, owner call.) This covers every Try again or Retry button that calls
  a query's `refetch()` (`grep -rn "refetch" frontend/src --include=*.tsx`): Trash, the Naming
  panel's load error since #232, `ErrorState`'s Retry, the import pages' notices, and more. They
  give no sign on a repeat failure: the same alert returns (after about a second where the query
  retries once; at once where it sets `retry: false`, as `useImport.ts:531,654` and
  `useAlbum.ts:59` do) and a screen reader announces nothing. On success the button disappears
  and keyboard focus drops to `<body>`. The one exception is the login page's "Check again"
  (`RecheckStatus`, `pages/LoginPage.tsx:778`), which shows "Checking…" while it fetches: the
  in-app pattern to copy. A fix belongs to all of them at once: a busy label while fetching,
  and a chosen focus target on success.

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

### From the 2026-09-21 pre-push review of `feat/import-keep-downloads`

Three seats reviewed that round's diff. Everything Critical/High/Medium was fixed on the branch;
these are what was left, with the measurement that produced each.

- **The failed-row banner sits ABOVE the `h1` the app focuses** (UI seat). On all three bank
  screens `FailedBanner` renders before the heading, and both `useDeferredH1Focus` and
  `RouteAnnouncer` put focus on `h1[tabindex="-1"]`. A screen-reader user arriving cold reads
  forward from the heading and never reaches the banner; heading navigation skips it because it
  sits under no heading. `role="alert"` should rescue it, but the region and its text enter the
  DOM in the same commit and an effect moves focus immediately after — the classic live-region
  miss. Nothing focuses this banner (verified: `useFocusAfterMutation` has one caller,
  `PlaylistDetailPage`, and no `StatusBanner` is focusable). Cheapest candidate is
  `aria-describedby` from the h1 to the banner; cost is that the long strerror gets spelled into
  the heading's description. **Needs a real screen-reader pass before choosing.** Preexisting;
  this round sharpened it by withholding the duplicate strip, which removed the only
  forward-of-h1 trace that something was wrong on the no-match screen.
- **The sticky control bar pins a doomed CTA while the instruction scrolls away** (UI seat). On a
  `fix_folder` no-match row, Use as-is / As tracks all queue an apply that fails identically until
  the folder is fixed, and `makeSubmit` navigates to `/review` on success so the failure arrives
  minutes later on another page. `ReviewControlBar` is `sticky bottom-0`; the banner is not. Do
  NOT fix by disabling them — Ignore must stay live, and a disabled bar plus a flag that only a
  rescan clears is a lockout. Now that a rescan discharges the row, "Fix the folder, then rescan."
  became an honest sentence; that is the likely fix.
- **`unreadable_reason` is safe by caller convention, not by type** (security seat).
  `app/import_jobs/runner.py` — every docstring in the chain justifies safety with "`str(OSError)`
  interpolates `exc.filename`", but `filename` is the wrong slot: for `OSError(errno, strerror,
  filename)` the CALLER chooses `strerror`. Measured: `OSError(EACCES, f"cannot read {path}")`
  returns `(cannot read /tmp/…/album)` — leaks. It is safe today because every raise site in the
  app passes a fixed strerror and puts the path in the third slot, and installed beets 2.13.1 has
  zero `raise OSError` with a custom strerror. `os.strerror(exc.errno)` would make it structural
  and also fixes the next item. **Corrected 2026-09-21 — the shape written here first was wrong:**
  bare `os.strerror(exc.errno)` raises `TypeError: 'NoneType' object cannot be interpreted as an
  integer` on exactly the errno-less subclasses the next bullet names (measured), turning a cosmetic
  string into a crash. It needs the guard:
  `os.strerror(exc.errno) if exc.errno is not None else "the reason was not reported"`.
  Search words: strerror, caller convention, unreadable_reason, os.strerror errno None TypeError.
- **`unreadable_reason` degrades to `errno None`** (security seat). `shutil.Error`,
  `shutil.SameFileError` and `urllib.error.URLError` are `OSError` subclasses built with one
  argument, so both `strerror` and `errno` are None and the operator reads "the server could not
  complete this row (errno None)". No leak, no information either. Reachability through
  `_apply_one` is thin. Covered by the `os.strerror` fix above **only in its guarded form**.
- **A non-`OSError` escapes both fingerprint guards on a non-UTF-8 filename** (security seat,
  2026-09-21; PREEXISTING — the arms are unchanged by the Sonar round). `app/bank/fingerprint.py:40`
  does `f"{rel}\n{size}\n{mtime_ns}\n".encode()`, but `os.walk` returns names decoded with
  `surrogateescape`, so an audio file with a non-UTF-8 name raises `UnicodeEncodeError` — not an
  `OSError`, so it escapes every `except` arm of both `api/bank.py::_current_fingerprint` and
  `bank/apply_runner.py::_row_stopped_by_folder_check`. Measured end to end through the rescan route
  with `raise_server_exceptions=False`: **500 `Internal Server Error`, and the folder path is NOT in
  the body** (`str(UnicodeEncodeError)` names the offending character and position, never the
  string), and `main.py` sets no `debug=True`, so no traceback is returned. So this is robustness —
  a 500 where a 409 was designed, plus a cryptic row error on the apply side — **not disclosure**.
  Remediation is one call, not a mechanism: `digest.update(os.fsencode(...))`, measured to round-trip
  the exact bytes the OS gave (`os.fsencode("02 tr\udcffack.mp3")` -> `b'02 tr\xffack.mp3'`) so the
  digest stays stable. NOT verified: the search route and a live apply drain were reasoned from the
  identical arm structure, not driven. Search words: surrogateescape, UnicodeEncodeError,
  fsencode, fingerprint, non-UTF-8 filename.
- **Two registers in one slot** (UI seat). `FailedBanner`'s muted line takes lower-case
  dash-joined continuations from `_row_error` AND capitalised standalone sentences from
  `unreadable_source_sentence` ("That folder can't be read. Permission denied."). `_row_error`'s
  docstring claims the slot reads "lower-case and dash-joined", which is now false for the arm
  this round added. beets' own `state.error` lands there too, so the mixing predates this round;
  the docstring is the part worth correcting.
- **`FAILED_HEADLINE[item.error_recovery]` has no fallback** (UI seat). The old `!== false`
  predicate was undefined-safe; a direct index is not. Only triggerable in dev against a stale
  separate backend — one Docker image ships both halves, and the legacy validator fills old rows
  on read — and the failure is a blank headline with the muted reason still shown. `??
  FAILED_HEADLINE.decide_again` is one token if it ever bites.
- **`--sidebar-ring` is an unconsumed third copy of the ring violet** (`styles.css`). Declared and
  mapped to `--color-sidebar-ring`, used by no utility; staged for the Phase 2 sidebar per its own
  comment. It is the token a future ring change silently misses — the 2026-09-21 alpha sweep did.
  Either delete it or note that it must track `--ring`.
- **The ring-alpha pin test matches class names in COMMENTS, not just class strings**
  (`ui/checkbox.test.tsx`). It scans source bytes through `?raw`, so a comment quoting
  `focus-visible:ring-ring/70` counts as a site (13 seen, 11 real). Harmless today and the
  `>= 10` control is satisfied by real sites alone, but a future comment mentioning `/50` fails
  the test confusingly. Comment-stripping across TS+CSS was judged more complexity than it buys.
  Related trap, measured the same day: spelling a utility literally in a comment makes Tailwind's
  scanner EMIT it — a dead `.focus-visible\:ring-destructive\/20` rule reached `dist` that way.
- **`_SUMMARY_EXCLUDE`'s comment reads as a complete list and is not** (`app/bank/store.py`).
  `_summary_of` builds `BankItemSummary(**item.model_dump(exclude=_SUMMARY_EXCLUDE))`, so
  `error_recovery` is passed as an unknown kwarg and dropped by pydantic's `extra="ignore"` —
  exactly what `error_retryable` did before, so no regression. The comment ("the heavy fields a
  summary drops") is what misleads.
- **The 503 sentence sends a benign concurrent start to inspect a healthy share** (quality seat).
  `import_.py` — the comment states the server cannot tell a second start from a stat that never
  returned, then optimises the copy for the wedged branch alone. Two genuinely concurrent starts
  (the inbox route plus a manual POST, the first finishing normally) now yield only "Check that
  your music share is responding." The owner chose this wording deliberately on 2026-09-20
  ("name what's true"); recorded because the justification in the comment argues both ways.

- **The duplicate route's 404 sentence does not name the finished-job case** (2026-09-19). After a
  stop releases a parked duplicate, `GET /import/{job}/albums/{i}/duplicate` 404s like the cover and
  the decision routes; its OpenAPI description still reads "No import job has that id, or no
  duplicate is parked at that index." — true once released, but silent on why. The candidate route
  carries the same sentence. Reword both on the next contract-touching round (a regen of
  `openapi.json` and `schema.d.ts`); wording only.
- **The paused sweep's fallback CTA is solid while every other start-again CTA is outline**
  (design seat, 2026-09-19). `SweepDoneCta` renders its `/import` "Import another folder" fallback
  with the default variant; `JobFailed`, `JobNotFound` and the stopped panel all use outline. One
  token; not on this branch's diff.
- **The done panel's counts line omits set-aside** (UI seat, 2026-09-19): "1 album imported · 0
  skipped" over a Needs-review row. Add the set-aside count to the line, or drop the line where a
  row already says it.
- **The stopped panel's "Add from folder" could prefill the path** (UI seat idea, 2026-09-19). The
  run knows its folder; the CTA sends the user to an empty form. Small; only if the folder browser
  of branch 2 does not make it moot.

- **Settings → Trash's per-row Empty confirms with "Delete permanently?" / "Delete"** (2026-09-14,
  PR #227 browser pass). The trigger's accessible name is "Empty <album>", Empty all's dialog says
  "Empty the whole Trash?" / "Empty all", and Restore's refusal says "Empty removes it", while the
  row's `ConfirmAction` in `SettingsTrashPage.tsx` is titled "Delete permanently?" with a "Delete"
  button. One verb for one action; wording only.

- **A confirm dialog's focused control drops focus to `<body>` when it is disabled while
  pending.** (Family trait, read by the UX seat 2026-09-12; Chromium blurs a focused control on
  `disabled`, jsdom does not, so no test can see it.) `ArtistArtStatus`, `DeleteAlbumAction`,
  `ConfirmAction` and the new Reset confirm all disable Cancel and the action for the duration
  of the request. On the error path focus sits on `<body>` until the next Tab, when the
  FocusScope pulls it back inside. Fix as a family or not at all: `aria-disabled` plus an
  early-return guard in the handler, on all four at once. Not a regression — the Reset confirm
  copied the family.
- **`useStartArtistArtBackfill` carries no start bound.** The Apply start got a 10 s bound
  (`fix/art-apply-keeps-hand-placed-art`) because a stalled start latched the confirm dialog;
  the backfill start on the Settings panel is a plain button with no dialog, so a stall greys
  one button. Add the same `withStartTimeout` when the panel is next touched. Also: the hook
  test's `10_000` is a literal, not the module-private constant — a changed bound hangs that
  test instead of failing it.
- **`tests/test_cited_shas.py` reports every prose cite at line 1.** A `.md` file is read as one
  chunk at `lineno` 1, so a failing cite in BACKLOG.md prints `BACKLOG.md:1` four times for four
  different lines (seen 2026-09-11 when a branch-local sha was cited); the `.ts`/`.py` paths count
  lines. Fix shape: count newlines before `match.start()` the way the block-comment path already
  does. Cosmetic — the guard still fails correctly.
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

  ~~**Two selection controls do NOT pass**~~ — **CLOSED 2026-09-11** (on
  `fix/phone-width-rows-and-hit-areas`, PR #221, squash `a053ffc` = v0.51.2; vault decisions 40).
  (The blank line above is load-bearing: without it markdown lazy-continuation pulls this
  closure into the `SegmentedControl` bullet's own paragraph, so that OPEN entry reads as
  closed. Not struck — `~~` is inline and cannot reach backwards over the bullet.)
  Measured with `elementFromPoint`, walking outward from each control's centre, at all **six**
  `Checkbox` call sites and the native radio; nothing moved (every row rect identical) and the
  drawn box did not change.
  **"The crops hashed identically" was false, and is restated as the measured delta**
  (re-measured 2026-09-11 against a build of the merge base `b8fb9f4`). 36×36 crops centred on
  the control, base vs branch: the checkbox's crop differs in **54 of 1296 px unchecked (max
  1/255)**, 26 checked (max 6/255), 66 focused-unchecked (max 2/255) and 46 focused-checked
  (max 19/255). The radio, whose fix is a wrapping `<label>` and no `relative`, hashes **0 of
  1089** in both states. What holds is the geometry and the reach, not the bytes: **the drawn
  rect is identical in all four states**, no differing pixel is further than **3px** from the
  drawn box (inside the focus ring's own reach; on the two unfocused crops none is outside the
  box at all), and the pseudo-element computes to `content: ""`, 24×24, `position: absolute`,
  transparent background, 0 border — it paints nothing. The cause of the delta is `relative`,
  which moves the control into the positioned paint layer and re-rounds its antialiasing.
  (Two of these four counts also differ from the ones first recorded on 2026-09-11 — 33 and 34
  — because the focused crop lands on a different row, at a different sub-pixel y. Any such
  count is per-crop; the rect and the 3px bound are what to check.)
  `Checkbox` 16×16 → a **24×24** target
  from a pseudo-element in the primitive: `BankSection.tsx:291` (Select all) and `:501` (bank
  row — `:495` was the pre-branch line, re-derived 2026-09-11) 24×24,
  `ImportPlaylistsPage.tsx:409` 24×24, `MergePlaylistDialog.tsx:148` 24×24,
  `PlaylistDetailPage.tsx:583` 51×24 (its wrapping label is wider), `BrowsePage.tsx:167`
  **20×24 — the one that did not reach 24 in both axes**, because the facet rail is an
  `overflow-y-auto` scroller whose content box started at the checkbox's own left edge and
  clipped the pseudo's left 4px (`overflow-y` alone makes `overflow-x` compute to `auto`).
  **Closed the same day at zero measured cost** — its struck entry under *Open bugs* carries
  the mechanism and the 201-width sweep. `/duplicates`' keeper radio 13×13 → **45×37**, from a wrapping
  `<label>` rather than a pseudo-element: Chromium renders `::before` on an `<input>` and
  Firefox does not. **No enlarged area overlaps another control**: every control on all five
  pages was hit-tested at its centre, its four edge midpoints and its four quarter points — 0
  points taken by a neighbour. Below is the original entry.

  **Two selection controls do NOT pass** (measured 2026-09-08 and 2026-09-11). `Checkbox` (on
  `/review`'s bank rows): the shadcn primitive is `size-4`, so its hit rectangle is **16×16**,
  under SC 2.5.8's 24px Level AA minimum. That is the primitive's floor and it is app-wide, not a
  bank-row property — every `Checkbox` call site inherits it; a fix belongs in
  `components/ui/checkbox.tsx` (a pseudo-element hit area, so the visual box does not grow), not
  at a call site. `/duplicates`' keeper radio is smaller still: an unstyled
  `input[type="radio"]`, so its box is the UA's — **13×13** in Chromium, measured at all 201
  widths — and it is the only selection control on that page. Same class of decision as the three
  above and the owner's to make.
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
- `.thumb.src`/`.thumb.bin` pair is not atomic as a unit (a tag-revisit corner when the
  override slot changes under a served thumb); served-tag vs. served-bytes TOCTOU is inherited from the cover cache class (fix:
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
- ~~The shipped starter-config header makes two false claims.~~ **CLOSED 2026-09-23** on
  `feat/import-keep-downloads` (PR #232): the header now says the Settings pages edit the file and
  Apply loads it. Existing files keep their old header. It became the header of the
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

- **Duplicate screen at 320 px: the page scrolls sideways by 10 px, and the "Importing (new)"
  card's text column collapses beside the cover** (measured 2026-09-19 in Orca's browser on the
  scratch server: docScrollW 315 vs clientW 305; the track-comparison table measures 449 px inside
  its own scroll container, so the 10 px come from elsewhere on the page; the card's text shows two
  to five characters per line — "D.", "C..", "1967 · 1 track" wrapped word by word). Seen while
  verifying the post-Apply hop; not that change's element. `DuplicateReview.tsx`.
- **Bank review page: two message lines mount WITH their text, so a screen reader may not
  announce them** (`BankReviewPage.tsx` `messages` — the collision line and the unpinned-option
  note). The same idiom was fixed on the candidate screen and in `ReviewControlBar`'s
  "Checking your library…" region (always mounted, `sr-only` while empty, text swapped) in the
  post-Apply hop change; this page was left as found. Same fix, one page.

## Recently shipped

- **The Trash path resolves every link itself and refuses one whose target reaches into the
  music library; the Trash read routes ask the writes' question — PR #227, squash `0824d0a` =
  v0.51.7 (2026-09-14).** The walk behind `MUSICDROP_TRASH_DIR` opens every component
  `O_NOFOLLOW`, reads a link with `readlink` and walks its target hop by hop under the kernel's
  40-hop budget (`store_layout._Chain`). A target inside the music library is refused unless it
  IS the root (the alias spelling); a target outside keeps working (owner ruling 2026-09-13,
  `decisions.md` #46). The refusal names the link and where it points. `GET /api/trash` and the
  reorganize preview take `checked_reachable_store_dirs` and answer 503 where they used to list
  wherever the path resolved. A FIFO, socket or device in Trash is skipped by the listing,
  refused by Restore (a 503 naming the file inside the entry, or saying the entry itself is not a
  folder or a regular file) and refused unopened by the origin-record reader,
  which reads one `O_NONBLOCK` descriptor capped at 64 KiB; the delete side unlinks a plant only
  on proof it is not a record.
  Behaviour changes: an operator link into the library is refused on every route; Empty on an
  entry whose record key is a pipe, device, leased or oversized file returns at once; the
  Reorganize panel shows the layout refusal's sentence; more than 40 links is refused.
  **Upgrade note.** A link in the Trash path whose target climbs out of the music library with
  `..` (`ln -s "$MUSIC/../elsewhere" /srv/x`, Trash at `/srv/x/.trash`) is now refused on every
  route, the Trash page included, with a 503 naming the link; spell it without the detour. Six
  such spellings the pre-branch code accepted now refuse; a target that lands on the root or
  stays below it is unchanged.
  Recorded, not fixed: the bind-mount half of the Trash chain, boot accepting a chain the routes
  refuse, the create loop carrying its below-the-root decision rather than re-asking per created
  part, the listing's stat-then-open window, the record reader's device-open window, and a
  non-empty directory at a record key.

- **Art, lyrics and move-asides are anchored on directory descriptors, and a symlinked folder
  below the music root is refused — PR #226, squash `6e02fc4` = v0.51.6 (2026-09-13).**
  Every syscall for a file under the music library — the artist-art writer, the lyric sidecar
  writer (its reads and its one delete included) and the move-aside that carries a replaced file
  into Trash — goes through a directory descriptor opened once per folder, with every component
  below the root opened `O_NOFOLLOW`; a symlinked artist or album folder is refused with one
  WARNING instead of followed, and the root itself may still be a link (owner ruling 2026-09-12:
  bind mounts are the supported spelling for spanning disks). One atomic writer
  (`app/playlists/atomic.py`) replaces five private recipes: an unpredictable
  `.<pid>.<16 hex><suffix>.tmp` created `O_EXCL|O_NOFOLLOW` and published through `dir_fd`. The
  Trash root is now created by the code that takes its identity, walked component by component
  through descriptors, and the identity the movers compare is `fstat` on the descriptor that walk
  reached — no re-stat by name between the walk and the move. Refused with a 503: a `..` in
  `MUSICDROP_TRASH_DIR`, a symlinked or non-directory part below the root, a spelling that
  reaches into the library without naming `directory:`, and a chain the `..` climb cannot finish;
  a Trash inside the library is created only while the music is really present, so a dropped
  share gets nothing on its bare mountpoint and blames `directory:` rather than the Trash
  setting. Settings → Beets runs the same walk read-only, so the page can no longer read healthy
  while every delete answers 503.
  The rounds' shape: six slices, then five review rounds (code + security seats), each fix round
  reviewed on its own diff and the last one prose-and-pins only. The seats moved real ground
  three times — the jump-in question had to be asked at EVERY component above the root (a link
  planted below an operator's link had moved the walk out first), an unfinished `..` climb had to
  refuse rather than read as "outside", and a present-but-unreadable `0o111` music root had to
  keep anchoring the walk by `stat` where `open_root` answers EACCES.
  What the CI-red taught: the one failure that appeared only on the runner was inode REUSE. The
  cross-device move-aside re-lstat'd its destination and compared `(st_dev, st_ino)` alone; on
  the runner's ext4 a newcomer written at that path got the inode the symlink had just freed, the
  compare saw no change, and the final unlink took the newcomer. Identity is not enough for a
  name that was deleted and recreated — the compare now also asks type, size and mtime. A test
  whose premise is a value the OS chooses (an inode, a temp name) passes locally and is decided
  by the runner.
  Residuals recorded rather than fixed: the three folder movers still open the Trash root by
  path; the import-time folder mover is the one creation site outside the checked creation; a
  bind mount of a library subdirectory at an outside path reads as outside to the `..` climb; and
  the link-target shape the branch above this one closes.

- **Reset to auto confirms first and moves the uploaded portrait to Trash; Trash lists
  moved-aside files as their own row — PR #225, squash `9e918e6` = v0.51.5 (2026-09-12).**
  `POST /api/artists/image/reset` sits behind an AlertDialog ("Reset to auto?"); an uploaded
  or pasted override moves into a Trash entry with a `files` origin record before the
  automatic slot is cleared, so no path unlinks a person's upload. The move and the clear run
  under the beets swap lock: 409 while the lock is held (the lock half of the library-busy
  check, `raise_if_swap_lock_held`) or while the art sweep runs, the sweep gate asked again
  with the lock held; 503 saying the reset stopped when a move fails part-way, with the OS
  strerror and no server path. Trash rows for `moved="files"` entries read "Files moved
  aside." with the folder they were at and Empty as the only action; the header no longer
  promises a restore location; container names built from tags share the display-name
  neutraliser (`_one_trash_level`: separators, NUL, U+FFFD, leading dots).
  Cleanup the rounds forced: `ArtistImageCache.clear_override` deleted (no production caller),
  the reset route's OpenAPI description cut to one sentence, six universals ("and nothing
  else", "nothing sweeps it") corrected and pinned, the lock re-check pinned on lock STATE per
  read rather than a read count.
  Closes the struck Reset-to-auto entry and the struck art-container-row entry above.
  Recorded and not fixed: the filler's ungated auto-slot write, the mid-move upload bound
  (never lost), the cover route's 500 on an unparseable track, and the descriptor anchoring
  this branch takes up.

- **Save art confirms first and moves replaced art to Trash — PR #224, squash `a8b08d5` =
  v0.51.4 (2026-09-11).** `POST /api/artists/art/apply` sits behind an AlertDialog that names
  what it writes and says existing files move to Trash first, and the start request carries a
  10 s bound so a stalled start cannot latch the dialog open.
  The `artist-poster.*`/`artist-background.*` a forced write replaces go into one Trash entry
  per artist folder (`<folder> - artist art`, origin record `moved="items"`, renamed to
  `moved="files"` by PR #225), claimed by a bare
  `mkdir` so a directory that arrived after the allocator looked raises before any move;
  nothing is written into a folder whose old files did not all move aside, and
  `write_artist_art` reports `failed` as soon as one write or move-aside errored while
  `written` still counts what landed.
  A rename now writes art only where it is missing — both rename call sites pass `force=False`,
  so merging artist A onto B no longer replaces B's curated poster, and the rename dialog names
  the art write only when the toggle is on.
  Hardening the review rounds forced: the art and lyrics writers create unpredictable
  fixed-length temp names with `O_EXCL|O_NOFOLLOW` (a symlink planted at the old derived
  `.<name>.tmp` was followed and published as the destination — measured), and all five
  parent-dir fsync opens carry `O_DIRECTORY` (a FIFO swapped in there blocked forever — 2 s
  measured, no error).
  Closes the struck Save-art entry and the struck partial-`status` entry above. Recorded and
  not fixed: the two descriptor-anchoring windows (the per-artist store check, and the
  container claim to the first move), the Reset-to-auto gap (closed by PR #225), the
  moved-aside Trash row's wording, the `.<pid>.<16 hex>.<ext>.tmp` dotfile a killed write
  leaves, and the three derived-name writers left untouched — each under its own entry or
  inside the closure it came from.

- **The lint gate reads the newest analyzer bundle — PR #222, squash `1de4a0e` = v0.51.3 (2026-09-11).**
  The bundle-on-disk check in `frontend/eslint.config.test.ts` now picks the newest
  `sonar-javascript-plugin.jar` in the scanner cache by mtime; it used to take the first of the
  hash-named directories in `readdirSync` order, which became a coin toss the day the
  SonarQube 26.9 upgrade left two. The deliberately unpinned `@typescript-eslint/eslint-plugin`
  version the config comment quotes (8.67.0 in analyzer 13.8) now has a reader that compares it
  to the bundle only, never to node_modules. Records #221. A `test` squash still cut a patch
  release, because the release keys on changed paths and `frontend/` moved.

- **Phone-width rows and hit areas — PR #221, squash `a053ffc` = v0.51.2 (2026-09-11).**
  Decisions 39–42, each browser-measured with a real scrollbar, 201–237 widths per surface. A row's actions
  drop below it under 28rem of row width on the bank, decision and parked feed rows (a grid with
  a container query on the row; the title keeps its ellipsis at every width, and the sliced `Open`
  and the badge ink under `Resolve` go with it). 28rem rather than the 20rem first derived from
  the typographic floor: at viewport 392–430 the inline title showed 6–10 characters and the
  stacked one 18–23, and the 473px narrowest desktop row clears 448. The `Checkbox` primitive draws a
  centred 24×24 pseudo-element so every caller meets WCAG 2.2 SC 2.5.8 without growing; the
  `/duplicates` keeper radio gets a padded label; `/browse`'s facet rail stops clipping the target
  at no measured cost. The `/duplicates` "most complete" badge moves to the meta line (keeper
  title 0 → 114px at 320, equal to its sibling at every width; the badge wraps at 320 and never
  truncates), and keeper radio names carry format, bitrate and the badge text, never a
  placeholder. The struck entries above that cite decisions 39–42 are its closures. Recorded and
  not fixed — three under their own entries, three inside the closures they came from: the bank
  row's status badge still owning the title line (1–3 characters of title at viewport 320–352 and
  513–543, 23–24px at 320, read by the UX seat); the stacked badge under the meta's start at 35
  widths; the two dropped arms on `/review` not sharing a left edge; a focused feed-row action
  unmounting when the live feed applies its album (pre-existing, unmeasured); the done panel's
  `readOnly` asymmetry; the sha guard's line-1 prose cites. The folder path in a keeper radio's
  name, when two non-suggested members match, stays the owner's call.
- **Import-feedback residuals — PR #220, squash `b8fb9f4` = v0.51.1 (2026-09-11).** The five
  residuals #217 left, re-read off `git log -1 --format=%B b8fb9f4` (2026-09-11: this entry had
  named four, and three of them were defects #220 FOUND rather than residuals it closed):
  one `SEGMENT_SEP` dialect for the `<confidence>% · <match>` line, so the Review row and the
  import feed stop spelling the same string two ways; the candidate page's match header wrapping
  as a sentence instead of squeezing to three line boxes at 360px; `awaiting_decision` answered
  from the park itself rather than a consumer-side index set (struck above); the feed's and the
  sweep's byte-identical status lines extracted into one shared `StatusLine`; and a paused
  sweep announced at the press instead of up to ~5s later.
  What it FOUND and fixed along the way, among them (each its own struck entry above): a failed
  bank row's error moved out of `AlbumRow`'s `shrink-0` meta slot onto its own line;
  **`AlbumRow`'s subtitle/meta line** clipped to its column, so the `shrink-0` meta span's ink
  stops taking taps meant for `Ignore` (at 320-376 that ink ran past the text column and
  painted inside the button's hit rectangle); the select checkbox re-centred on the row it
  selects instead of on row+error; `/duplicates`' keeper radio re-centred the same way; and the
  candidate page's h1 stopped pushing the document 371px sideways at 360px (`min-w-0` AND
  `break-words` — either alone measures 371). The widths it measured but did NOT fix — the
  0px title, the sliced `Open`, the mid-word meta cut — are the struck entries above, closed by
  decisions 39 on the branch after it.
- **Import feedback — PR #217, squash `f08bc66` = v0.51.0 (2026-09-08).** How long a run has
  taken and when it needs you. `ImportJobState` gains two server-side fields: `elapsed_seconds`
  (the server's own monotonic clock, so a reload does not restart it and the browser's clock is
  never consulted; it keeps counting while a job is parked and freezes at the first terminal
  transition) and `awaiting_decision` (true while the worker is blocked in `park()` — not
  inferable from row statuses, because an unattended duplicate skips without parking and a
  `search` re-lookup keeps its row `needs_review` while beets works). The import page, its
  status line and the empty state read both. The residuals it left are #220's entry above; the
  four it recorded but did not fix are struck or open under their own entries.
- **Trash and store containment — PR #215, squash `35d37bc` = v0.50.2 (2026-09-05).** Vault decision 35: a Trash inside the
  music library is allowed; one that is or contains the music library, the beets dir, the origin
  store or an app folder is refused at startup, on Save/Validate/Apply and at every destructive
  route, by one table over resolved paths plus an inode guard at the point of destruction. Empty
  Trash removes through the descriptor it checked. Closes the three struck entries above; the
  residual list under them is the one place the accepted leftovers live.
- **Sign-in form field marking — #214 (2026-09-04).** Owner ruling `decisions.md` 31, asked after
  v0.50.0: every password form marks a field invalid only while the error shown is about that
  field. The sign-in form had kept painting its one field red for every answer (429, 503, the
  server-unreachable sentence); now a refused password marks and selects it, the other answers
  show their message with the field unmarked, retyping clears the refusal, and a 401 about the
  server re-checks status so the notice that names the fix replaces the form. Follow-up to #212.
- **First-run password setup — #212 = v0.50.0 (2026-09-03).** A fresh install sets its
  password on the sign-in screen (setup form; hash stored at `<beets_dir>/password-hash`,
  0600, atomic) and changes it in **Settings → Account** (wrong current password answers 403
  and keeps the session; a change re-mints only the caller's cookie). `MUSICDROP_PASSWORD_HASH`
  stays as an override that wins while set, readable or not, and both screens say what to fix
  or unset (`decisions.md` 29). `AuthStatus.password_source: none | env | file`;
  `POST /api/auth/setup` is the fifth exempt path, 409 once any source exists, under an
  in-process lock. Review-round hardening: the stored hash is read only when the entry is a
  bounded regular file (a FIFO no longer wedges the event loop, a dangling symlink counts as
  present); a 422 no longer echoes the submitted body; setup and change each write a line to
  the container log through uvicorn's logger; the boot line names a file the override shadows;
  the CLI refuses whitespace-only passwords like the routes; a process-level test floor keeps
  the suite green when a real `password-hash` sits in the dev beets dir; one field-marking
  rule on both forms with focus placed after every answer. The residuals it accepted are
  under *Accepted residuals* (the setup window re-opens whenever the file is absent; a lost
  data volume loses the credential; root-owned-file recovery UNVERIFIED).
- **Trash data-safety, two slices — #208 = v0.48.0 (2026-09-02) and #209 = v0.49.0 (2026-09-03).**
  - **#208 = v0.48.0 — undoable deletes.** Every mover records where an entry came from in a
    sibling store (`<beets_dir>/trash-origins/<entry>.json`, freedesktop `trashinfo` shape), so
    Restore puts a folder back exactly where it was instead of re-importing; a symlinked Trash
    entry is shown as refused with Restore and its own Empty disabled; the presence check
    refuses a delete when the music share is not mounted, even behind a stray file on the
    mountpoint. `decisions.md` 27 amended, 28 recorded from the owner's four answers.
  - **#209 = v0.49.0 — fail closed, move back.** `decisions.md` 28 items 3 and 4: a delete
    answers **503** and moves nothing when the origin store cannot be made, searched or
    written (three real probes before any mover runs; the message names the store, not its
    path, and reads "…Nothing has been deleted. Fix its permissions or its mount, then retry.");
    when `album.remove` fails after a whole-folder move the folder is moved back and the 500
    says so, hedging on the library rows because a plugin listener can raise after beets
    committed. The flat-library presence check samples the files the library names, not their
    folders. Also the first Sonar sweep of the Python side since 2026-08-31: 34 `S5778`
    sites hoisted to one call per `pytest.raises` and one `S112` annotation, so the persistent
    project reads zero open after the merge. Residuals stay above: the per-item mover has no
    undo, a listener failure orphans item rows, the leaked probe dotfile is not swept, the UI
    renders only the message half of a structured 500.

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
