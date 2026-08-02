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

- **Non-UTF-8 paths 500 JSON endpoints (app-wide, pre-existing).** `os.fsdecode` turns
  undecodable path bytes into lone surrogates; Starlette renders JSON with
  `ensure_ascii=False` and UTF-8-encodes, which raises → any library path that is not valid
  UTF-8 500s `GET /api/reorganize/preview` today (proven on main with an executed probe: an
  undecodable *directory* name, zero collisions involved). Fix is one `display_path()` helper
  (`os.fsdecode(p).encode("utf-8", "replace").decode()`) applied at every path-to-wire
  boundary: reorganize `_rel_to_music` / `_commonpath_of_dirs` / `_track_desc` /
  `_occupant_desc` / `_verify_moves`, `OrphanFolder`, `disk_sync._rel_path` — and audit the
  rest of the app for more. Trap for the tester: probe through `TestClient`, never bare
  `json.dumps` (its default `ensure_ascii=True` escapes surrogates and hides the bug).

- **Edit preview shows a collision-bound rename as a plain move.** Apply refuses it with an
  honest per-track error (#124 wave), but the preview still says "1 file will be moved" —
  the user learns at apply time. Needs the collision pre-flight mirrored into
  `preview_album_edit` and a conflict signal in `AlbumEditPreview` (contract change:
  openapi.json + gen:api).

## Open bugs / hardening

- **Album art can still be silently diverted.** The collision pre-flight covers item files
  only; `Album.move_art` goes through the same beets `unique_path`, so two album rows
  resolving to one folder with disjoint track names would land `cover.1.jpg` silently.
  One-shot (no churn — the unit is item-settled next run), which is why it was descoped.

- **CSRF/Origin posture on state-changing POSTs.** `/api/reorganize` (starts a library-wide
  move), `/stop`, and `/dismiss` are all CORS-simple POSTs with no origin check
  (`verify_upload_origin` covers multipart uploads only). Harden all three together or
  accept the posture deliberately — do not fix one route in isolation.

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

## Recently shipped

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
