# Repairing absolute item paths in `library.db`

One-time database repair, to be done in the same maintenance window as the code fix that
stops the problem recurring (`fix: store item paths relative to the music dir, like beets does`).

If you are reading this a year from now with no memory of any of it, sections 1 and 2 are
enough context. Section 3 is the rule that decides whether this goes well.

---

## 1. What went wrong

beets stores each track's path in `library.db` **relative to your music directory** —
`Nick Cave/Let Love In/01 Do You Love Me.mp3`, not `/music/Nick Cave/...`. That is deliberate:
it is what lets the library survive the music directory moving to a new mount, a new dataset
name, or a new machine.

beets only does that relativising when a `music_dir` context variable is set, and it sets that
variable **in the thread that opens the library and nowhere else**. MusicDrop opened the library
in the FastAPI lifespan and then ran imports on a plain worker thread, which starts with an empty
context. So every row MusicDrop's importer wrote was stored **absolute**. Every release from
v0.1.0 through v0.34.0 behaved this way; it was never a regression, it was the original bug.

**Nothing is damaged and no music was lost.** Every absolute row points at the correct file, and
will keep working for exactly as long as the music directory stays at the path it is at today.
This is a loaded gun, not a wound. Section 8 says what pulls the trigger.

Measured on the production library during the 2026-08-15 investigation:

```
items.path      ABSOLUTE 22,100 | relative 3,476
albums holding BOTH forms:  123
```

---

## 2. Measure your own box first — the census

Read-only, safe to run at any time, on a live library. Set the two variables to the
**container-side** paths if you run this inside the container, or to the host paths if you run it
on the host — the two forms are compared in section 4, and getting them mixed up is the one way
to waste the repair.

```sh
DB=/data/beets/library.db
MUSIC_DIR=/music

sqlite3 -readonly "file:${DB}?mode=ro" "
SELECT CASE WHEN substr(hex(path),1,2)='2F' THEN 'ABSOLUTE' ELSE 'relative' END AS form,
       count(*) AS rows
  FROM items
 GROUP BY form;"

sqlite3 -readonly "file:${DB}?mode=ro" "
SELECT CASE WHEN artpath IS NULL               THEN 'NULL'
            WHEN substr(hex(artpath),1,2)='2F' THEN 'ABSOLUTE'
            ELSE 'relative' END AS form,
       count(*) AS rows
  FROM albums
 GROUP BY form;"

sqlite3 -readonly "file:${DB}?mode=ro" "
SELECT count(*) AS mixed_albums FROM (
  SELECT album_id
    FROM items
   WHERE album_id IS NOT NULL
   GROUP BY album_id
  HAVING min(substr(hex(path),1,2)='2F') <> max(substr(hex(path),1,2)='2F')
);"

sqlite3 -readonly "file:${DB}?mode=ro" "
SELECT count(*) AS outside_rows
  FROM items
 WHERE substr(hex(path),1,2)='2F'
   AND substr(CAST(path AS TEXT),1,length('${MUSIC_DIR}' || '/')) <> '${MUSIC_DIR}' || '/';"
```

`0x2F` is `/`. A path stored as an absolute path is one whose first byte is a slash — that is the
whole test, and it works whether the column holds a BLOB or TEXT.

Query 4 counts rows that are **legitimately** absolute because the file lives outside the music
directory. Those must stay absolute and beets' migration correctly leaves them alone. Use the
`substr(...) <> ...` form rather than `NOT LIKE '<dir>/%'`: in SQL `LIKE`, `_` is a single-character
wildcard, so a music directory containing an underscore silently under-counts.

`mixed_albums` is the number that tells you this is not academic. An album holding both forms is
one whose rows disagree with each other, and 123 of yours do.

---

## 3. THE RULE: the code fix and this repair land together

**This is the most important sentence in the document. Do not do one of these without the other.**

When you re-import something already in the library, beets looks up the existing rows by path so
it can replace them instead of adding a second copy. That lookup builds its query pattern in
whatever form the *calling thread* is in, and matches a row only when the query's form and the
row's form agree:

| worker thread | row stored | match |
|---|---|---|
| bound (fixed code) | relative | hit |
| bound (fixed code) | absolute | **miss** |
| unbound (old code) | relative | **miss** |
| unbound (old code) | absolute | hit |

So today, with old code and a mostly-absolute database, the two sides *accidentally* agree for
most of the library. Change exactly one of them and they disagree for all of it. Measured in a
lab library of 83 items built to the same shape (57 absolute / 26 relative), counting how many of
the library's own files the import worker's replacement lookup can still find:

```
database     import worker             rows the lookup finds
UNREPAIRED   unbound (today's code)      57 / 83     <- status quo
UNREPAIRED   bound (the fix)             28 / 83     <- fix deployed, DB not repaired: WORSE
repaired     unbound (today's code)       2 / 83     <- DB repaired, fix not deployed: MUCH WORSE
repaired     bound (the fix)             83 / 83     <- both: correct
```

A miss means the importer does not see the row it is replacing, so it adds a second one. Both
single-sided orderings are worse than doing nothing. **One maintenance window. No imports in
between.** MusicDrop stays stopped from step 1 of section 5 until the section 6 verification
passes.

---

## 4. Before you start: the dry run that defuses the one-shot trap

beets repairs this with a migration it runs **once** and then records in a `migrations` table. If
it runs against the wrong `directory` it does nothing, says it succeeded, and records itself
anyway (section 7). The trap is entirely avoidable, because you can predict the result with a
read-only query first.

**Step A — read the prefix your rows actually carry.**

```sh
sqlite3 -readonly "file:${DB}?mode=ro" \
  "SELECT CAST(path AS TEXT) FROM items WHERE substr(hex(path),1,2)='2F' LIMIT 3;"
```

These were written *inside the container*, so expect `/music/...`, not the host's
`/mnt/pool/music/...`.

**Step B — check that beets will resolve `directory` to exactly that prefix.**

`beet config` is not good enough: it prints the raw config value. If your `config.yaml` says
`directory: ../music` — the value MusicDrop ships as a starter — beets resolves it against the
config file's own directory, so under `BEETSDIR=/data/beets` it means `/data/music`, which is
**not** the `/music` bind mount. Verified: the migration follows that resolution faithfully, so a
stale relative `directory` is a live way to burn the shot.

The reliable read is to ask beets to print a path it has already expanded, using a row you know is
stored **relative** — for an absolute row it would just echo the stored value back and tell you
nothing:

```sh
# on the host
REL_ID=$(sqlite3 -readonly "file:${DB}?mode=ro" \
  "SELECT id FROM items WHERE substr(hex(path),1,2)<>'2F' LIMIT 1;")
sqlite3 -readonly "file:${DB}?mode=ro" \
  "SELECT CAST(path AS TEXT) FROM items WHERE id=${REL_ID};"   # -> Artist/Album/01 Title.mp3

# in the container, where `directory` means what it will mean during the repair
docker compose run --rm musicdrop sh -c \
  "BEETSDIR=/data/beets beet ls -f '\$path' id:${REL_ID}"      # -> /music/Artist/Album/01 Title.mp3
```

The second output is the effective music directory with the first appended. If the census showed
zero relative rows there is no such row to use, and step C below is your only check.

**Step C — the dry run.** Set `MUSIC_DIR` to the prefix from step A/B and count what the migration
would change:

```sh
sqlite3 -readonly "file:${DB}?mode=ro" "
SELECT count(*) AS items_that_will_change
  FROM items
 WHERE substr(hex(path),1,2)='2F'
   AND substr(CAST(path AS TEXT),1,length('${MUSIC_DIR}' || '/')) = '${MUSIC_DIR}' || '/';"

sqlite3 -readonly "file:${DB}?mode=ro" "
SELECT count(*) AS albums_that_will_change
  FROM albums
 WHERE substr(hex(artpath),1,2)='2F'
   AND substr(CAST(artpath AS TEXT),1,length('${MUSIC_DIR}' || '/')) = '${MUSIC_DIR}' || '/';"
```

**If `items_that_will_change` is 0, stop.** Your `directory` does not match your rows, and running
the migration now would burn the shot for nothing. In the rehearsal these two numbers were 55 and
8, and the post-repair column diff showed exactly 55 changed `path` values and 8 changed `artpath`
values — the dry run predicts the outcome exactly.

Also budget disk space: beets writes **two** full copies of `library.db` as pre-migration backups
(one before the items table, one before the albums table). Have 2× the size of `library.db` free
next to it, plus room for your own backup.

---

## 5. The repair, step by step

Run everything **from inside the container**, so that `directory` and the stored paths are in the
same namespace, and so that the beets doing the work is the same version MusicDrop itself uses —
that matters, because a different beets on the host may not carry this migration at all. The image
puts `/app/.venv/bin` on `PATH`, which is where beets' `beet` script lives; if it turns out not to
be found, call it by that full path.

Two path namespaces are in play and the commands below mix them deliberately. `docker compose`
lines and plain `sqlite3` lines run on the **host**, where the database is `./data/beets/library.db`
(from the `./data:/data` volume). Anything inside `sh -c` runs in the **container**, where the same
file is `/data/beets/library.db` and the music is `/music`. Adjust the host side to wherever your
compose file lives.

```sh
# 1. Stop MusicDrop. No imports, no UI, from here until step 7 passes.
docker compose stop musicdrop

# 2. Your own backup. Take one even though beets takes its own. With the server stopped a
#    plain cp is fine; `sqlite3 ... ".backup"` is the safer habit and works either way.
sqlite3 ./data/beets/library.db ".backup './data/beets/library.db.pre-path-repair-$(date +%F)'"

# 3. Deploy the code fix (the release containing
#    "fix(import): store item paths relative to the music dir, like beets does").
docker compose pull musicdrop

# 4. Clear beets' migration ledger so the migration is allowed to run again.
sqlite3 ./data/beets/library.db "DELETE FROM migrations WHERE name = 'relative_path';"

# 5. Confirm it is gone (expect 0).
sqlite3 -readonly "file:./data/beets/library.db?mode=ro" \
  "SELECT count(*) FROM migrations WHERE name='relative_path';"

# 6. Run the migration in a ONE-OFF container, with the server still down. Opening the
#    library is all that is needed — the migration runs on first open, and `beet ls` is a
#    read-only command that does nothing else. The grep drops its 25k track lines and keeps
#    the migration banner; do NOT redirect stdout to /dev/null, that is where the banner is.
docker compose run --rm musicdrop sh -c \
  "BEETSDIR=/data/beets beet ls 2>&1 | grep -iE 'Migrat|backup'"

# 7. Verify with the census — section 6. Do this BEFORE starting the server.

# 8. Only once the census is clean, bring MusicDrop back up.
docker compose up -d musicdrop
```

Use `docker compose run`, not `up -d` followed by `exec`: MusicDrop opens the beets library in its
own startup, so simply starting the server runs the migration itself — silently, into the
container log, with no chance to read the banner and no chance to stop if the dry run was wrong.
A one-off container keeps the migration in the foreground where you can see it.

Steps 1, 3, 6 and 8 are `docker compose` wrappers around the operation that was rehearsed; the
rehearsal ran `beet ls` directly with `BEETSDIR` set. See section 10 for exactly which parts were
executed and which were not.

Step 4 deletes two rows — `relative_path|items` and `relative_path|albums`. The migration covers
`path` on items **and** `artpath` on albums, so one run fixes cover-art paths too. That is why
this is better than a hand-written `UPDATE items`.

Expected output from step 6, in this order (the two backup lines really do print last — they go
through a different, buffered stream):

```
Migrating path for 25576 items...
Migration complete: 22100 of 25576 items updated
Migrating artpath for N albums...
Migration complete: M of N albums updated
Created database backup at: '/data/beets/library.db-before-items-relative_path.bak'.
Created database backup at: '/data/beets/library.db-before-albums-relative_path.bak'.
```

Both backups are written **before** the corresponding table is touched, despite printing last —
verified by checking that `library.db-before-items-relative_path.bak` still held the pre-repair
census. If you see no `Migrating` lines at all, the ledger was not cleared and nothing ran.

---

## 6. Verify — and the message that lies

**`Migration complete: N of M items updated` counts rows EXAMINED, not rows CHANGED.** It is
printed from the count of rows that *were* absolute, before any comparison with what they became.

This is not a subtle difference in wording. In the rehearsal, a run against the **wrong**
`directory` and a run against the **right** one printed byte-identical messages:

```
Migrating path for 83 items...
Migration complete: 57 of 83 items updated      <- wrong directory: nothing changed
Migrating artpath for 12 albums...
Migration complete: 8 of 12 albums updated
```

```
Migrating path for 83 items...
Migration complete: 57 of 83 items updated      <- right directory: 55 rows genuinely rewritten
Migrating artpath for 12 albums...
Migration complete: 8 of 12 albums updated
```

The message cannot tell you which one you got. **Verify with the census from section 2, never
with the message.**

A successful repair looks like this — rerun the section 2 census and compare:

```
                      before          after
items.path ABSOLUTE   22,100          only the rows outside the music dir (query 4's number)
items.path relative    3,476          everything else
mixed_albums             123          0
outside_rows               k          k   (unchanged — these are correctly absolute)
```

Rehearsal, same queries, on the 83-item lab:

```
### PRISTINE (pre-repair) ###          ### REPAIRED ###
items.path ABSOLUTE|57                 items.path ABSOLUTE|2
items.path relative|26                 items.path relative|81
artpath ABSOLUTE|8 NULL|4 relative|4   artpath NULL|4 relative|12
mixed_albums 3                         mixed_albums 0
outside_rows 2                         outside_rows 2
```

Second, confirm every row still points at a real file. This is the check that would catch a
migration that rewrote paths wrongly rather than not at all:

```sh
docker compose run --rm musicdrop sh -c "BEETSDIR=/data/beets beet ls -f '\$path' | while IFS= read -r p; do [ -e \"\$p\" ] || echo \"MISSING: \$p\"; done"
```

Expect no output. The loop has to run **inside** the container: the paths beets prints are
container-side, so on the host every one of them would report as missing. It stats every file, so
on a NAS it is slow; that is fine, run it once. The quoting is deliberate — `\$path` must survive
the outer shell *and* the inner `sh -c` to reach beets as a format string.

In the rehearsal the equivalent checks came out clean: every one of the 99 path-bearing rows
resolved to the byte-identical absolute file before and after (`0 rows changed target`), and a
full every-table, every-column, every-row diff of the pre-repair backup against the repaired
database showed only the two path columns moving:

```
albums    16 ->    16 rows, columns changed: {'artpath': 8}
items     83 ->    83 rows, columns changed: {'path': 55}
album_attributes / item_attributes / migrations: NONE
```

Those two numbers are exactly what the section 4 dry run predicted.

Finally, after the server is back up, import one album through the web UI and check that the new
rows are written relative. This is what proves the *code fix* landed, which the census cannot
tell you:

```sh
sqlite3 -readonly "file:./data/beets/library.db?mode=ro" \
  "SELECT quote(path) FROM items ORDER BY id DESC LIMIT 3;"
```

The values must **not** start with `X'2F`.

---

## 7. If it went wrong: recovering the burned shot

If the census shows nothing changed, the migration ran against a `directory` that did not match
your rows. It has recorded itself and will not run again on its own. Nothing is damaged. Recover
by clearing the ledger and retrying with the right `directory`:

```sh
sqlite3 ./data/beets/library.db "DELETE FROM migrations WHERE name = 'relative_path';"
# fix `directory` so it resolves to the prefix from section 4 step A, then reopen the library
```

Rehearsed: after **two** consecutive wrong-directory runs, a third attempt with the directory that
matched the stored prefix repaired the library completely (57 absolute → 2). The shot is not
actually one-time; it is one-time *per ledger row*, and you own the ledger.

The important corollary, learned by getting it wrong in the rehearsal: **`directory` must equal
the prefix the rows carry, not wherever the files happen to live now.** The migration never
touches the filesystem — it is a pure string operation. Verified: rows carrying `/music/...` were
repaired correctly by a beets run whose `directory` was `/music` **on a machine where `/music`
did not exist at all**. So if running inside the container is impossible, a throwaway
`config.yaml` on the host with `directory` set to the container-side path will do the job:

```yaml
directory: /music                # the container-side prefix the rows carry
library: /mnt/pool/appdata/musicdrop/data/beets/library.db   # host path to the real DB
plugins: []
```

```sh
BEETSDIR=/path/to/throwaway-beetsdir beet ls 2>&1 | grep -iE 'Migrat|backup'
```

Use a throwaway `BEETSDIR` for this, never your real one — a `directory` pointing at a
non-existent path is fine for the migration and wrong for everything else.

---

## 8. What breaks if you never do this

Nothing, until the music directory's path changes: a TrueNAS dataset rename, a different bind
mount in `docker-compose.yml`, or restoring the pool onto a new box. On that day:

- Relative rows follow the mount. Absolute rows do not — they point at a path that no longer
  holds the file.
- **`beet update` then deletes those rows, without a prompt.** Rehearsed: after renaming the
  music directory, `beet update 'album:Album 01'` printed `deleted` for all five tracks of a
  fully-absolute album and removed 5 item rows *and* the album row (83 → 78 items, 16 → 15
  albums). Every audio file was still on disk afterwards. What is gone is everything the database
  held: added date, play counts, flexible fields, lyrics, ratings, album grouping. A relative
  album in the same library, after the same move, resolved to the new location and lost nothing —
  `beet update` removed zero rows from it. (It did fail to *read* those files, because the
  rehearsal's "audio" is not real MP3 data. That error is a fixture artifact, not a beets
  behaviour; the point is that no row was deleted.)
- MusicDrop's own disk-sync performs the same removal but shows a preview first and never deletes
  audio — it calls `item.remove(delete=False, with_album=True)` (`app/beets/disk_sync.py:239`).
  The removal rows visibly climb out of the library root in the preview, which is the tell.
- Playlist exports become non-portable: `m3u_entries` writes each path relative to the playlist
  file, so a stale absolute row comes out as `../../…` and escapes the library root. Plex will not
  resolve it. Reorganize silently ignores those units — no move, no conflict, not counted as in
  place. (These two are from the 2026-08-15 investigation's lab, not re-run here.)

So the deadline is not a date, it is an event: **repair before any mount, dataset, or compose
path change** — and certainly before running `beet update` or MusicDrop's disk-sync after one.

---

## 9. Repairs that do NOT work

Do not spend a night on any of these. All three were rehearsed and all three failed to heal
in-place rows.

- **Waiting for a beets upgrade.** The migration is recorded in the `migrations` table and never
  re-runs on its own. Rehearsed: reopening the library with the ledger intact left the file
  **md5-identical**. Only `DELETE FROM migrations` unblocks it.
- **`beet move` / MusicDrop's Reorganize.** These heal only rows whose file *actually moves*.
  Rehearsed on the 83-item lab: `beet move` reported `Moving 2 items (81 already in place)` and
  healed exactly those 2 — the ones whose files were outside the music directory and got pulled
  in. The 55 in-place absolute rows were untouched. Nearly every row in your library is already
  at its computed destination, so this heals nearly nothing. (It also relocated the two
  outside-the-library files, which is beets working as designed and probably not what you want.)
- **`beet modify` / a tag edit.** `Model.store()` writes only the fields it considers dirty, and
  `path` is never dirty. Rehearsed: `beet modify -W -y id:1 comments='repair rehearsal'` wrote the
  comment and left the path `ABSOLUTE`. (A tag edit that changes a track's *computed destination*
  does move the file and does heal that one row — but that is a side effect, not a strategy.)

---

## 10. What was rehearsed here, and what was not

Rehearsed end to end on 2026-08-15 against a throwaway 83-item / 16-album beets 2.13.1 library,
built to the same shape as production (57 absolute rows including 3 mixed albums and 2 rows
legitimately outside the music directory, 26 relative rows, 8 absolute `artpath` values):

- all four census queries, the dry-run predicate, the sample-prefix query, and the
  relative-row resolution check in section 4 step B
- `DELETE FROM migrations WHERE name='relative_path'` → reopen → full repair, verified by census,
  by a per-row resolved-path diff, and by an every-column diff
- the wrong-`directory` trap, the identical success message it prints, and recovery from it
- `directory: ../music` resolving against the config file's own directory, and `beet config`
  printing the raw value rather than the resolved one
- repair driven with `directory` pointing at a path that does not exist on the running machine
- `beet update` deleting rows after a simulated mount move, and relative rows surviving it
- `beet move` and `beet modify` failing to heal in-place rows
- reopening with the ledger intact leaving the database byte-identical
- the missing-file loop, including its `sh -c` quoting, run without the `docker compose` prefix

**Not executable here, so untested:** everything wrapped in `docker compose` — `stop`, `pull`,
`run --rm`, `up -d`. The rehearsal ran `beet` directly with `BEETSDIR` set, which is what the
command inside `docker compose run` does. Also inferred rather than observed: that `beet` is on
`PATH` inside the image. The Dockerfile puts `/app/.venv/bin` on `PATH` and beets ships that
console script, but nobody has run it in the published image. If `docker compose run … beet`
reports "not found", call it as `/app/.venv/bin/beet`.

Check the `beet` invocation prints the `Migrating path for …` lines before trusting any wrapper.

The production numbers (22,100 / 3,476 / 123) come from the 2026-08-15 investigation's read-only
measurement of the TrueNAS library, not from this rehearsal.
