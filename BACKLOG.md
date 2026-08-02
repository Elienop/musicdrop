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

- **Edit preview lists outside-library tracks as "will move".** `preview_album_edit` builds
  move_plan rows from ALL items, but apply's `_inside_library` filter never moves an
  outside-library file (reports moved=False, no error). The new refusal pre-flight mirrors
  apply's filter correctly; only the move_plan row over-promises. Rare state, pre-existing.

- **Untested defensive lines** (deep-review survivors, all currently benign — pin when
  touched next): broken-symlink sidecar carry (`sidecars.py` `lexists`), singleton
  crash-path sidecar carry, `edit.py` `_inside_library` guard (pre-existing from main),
  disk-sync emptied-row first-dir-wins and `"."`-fallback, dismiss double-click swallow,
  the aria-disabled-not-disabled focus rule (the "Pagination rule" is convention, not test).

- **Disk-sync emptied-row path shows the first item's folder,** not the album root — a
  multi-disc emptied album reads `Artist/Album/CD1`. Still separates label twins (the
  feature's purpose); commonpath would be nicer.

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

## Recently shipped

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
