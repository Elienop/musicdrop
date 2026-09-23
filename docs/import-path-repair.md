# Repairing absolute item paths in `library.db`

One-time database repair, to be done in the same maintenance window as the code fix that
stops the problem recurring (`fix: store item paths relative to the music dir, like beets does`).

If you are reading this a year from now with no memory of any of it, sections 1 and 2 are
enough context. Section 3 is the rule that decides whether this goes well.

> **This repair was completed on 2026-08-15** on the production TrueNAS library, in one
> maintenance window with the code fix (PR #134, `e9c940c`, released v0.34.2 — tag verified).
> The dry run predicted 22,100 rows and the migration converted exactly that: items went
> 22,100 absolute → 0, the 123 mixed albums cleared, and the replacement lookup recovered.
> Two things the real run taught that the text below now reflects: the container-side prefix
> on that box was **`/library`**, not the `/music` used in every example (section 4 step A is
> what catches this — do it, don't trust the examples), and the **albums half printed only its
> backup line, no `Migrating` banner**, because its 21 surviving artpaths were already relative
> and 3,098 were NULL — which looks like section 5's burned-shot signature but is the healthy
> outcome for a table with nothing to convert (see the per-table note in section 5).
> The document stays as a runbook in case a future library needs it again.

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

Measured on the production library on 2026-08-15, by running the section 2 census on the
TrueNAS box itself. Not by the investigation that ran the same day — that one never had access
to this library and says so in its own report:

```
items.path      ABSOLUTE 22,100 | relative 3,476
albums holding BOTH forms:  123
```

---

## 2. Measure your own box first — the census

Read-only, safe to run at any time, on a live library.

**Where the SQL runs: on the host.** The published image has **no `sqlite3` command** — it is
built on `python:3.12-slim`, which ships the SQLite *library* but not the CLI. So every SQL step
in this document (this census, your own backup in section 5 step 2, the ledger `DELETE`, and every
verification query) runs on the TrueNAS host, against `./data/beets/library.db`. Only the `beet`
commands run in the container. **If the host has no `sqlite3` either, stop and read the Python
fallback at the end of this section before you start** — finding that out with the server already
down and the ledger already cleared is the one avoidable way to stall the maintenance window.

The two variables come from different namespaces, and mixing them up is the one way to waste the
repair. `DB` follows wherever you are running the command. `MUSIC_DIR` follows the **rows**: it
must be the prefix they actually carry, and they were written inside the container, so it stays
`/music` even when you run the query on the host. Section 4 step A reads that prefix off your own
rows — do that first if you are unsure.

```sh
DB=./data/beets/library.db     # host path (the ./data:/data volume)
MUSIC_DIR=/music               # the CONTAINER-side prefix the rows carry — not the host path.
                               # Generic example: the 2026-08-15 production run measured
                               # /library here. Read it off your own rows (section 4 step A).

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

**Fallback if the host has no `sqlite3`.** The container has no CLI either, but it does have
Python, whose `sqlite3` module is the same library. Write this file next to your compose file as
`data/repair-census.py` — it lands at `/data/repair-census.py` inside the container — and run it
there. It answers all four census queries above *and* section 4's step A and step C, so it is also
the one command to re-run after the repair:

```python
import sqlite3

DB = "/data/beets/library.db"    # container-side; this script runs in the container
MUSIC_DIR = b"/music"            # the prefix the rows carry

db = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
db.text_factory = bytes
items = db.execute("SELECT album_id, path FROM items").fetchall()
albums = db.execute("SELECT artpath FROM albums").fetchall()
db.close()

prefix = MUSIC_DIR + b"/"
absolute = [p for _, p in items if p and p[:1] == b"/"]
outside = [p for p in absolute if not p.startswith(prefix)]
art = [a for (a,) in albums]
by_album = {}
for album_id, p in items:
    if album_id is not None and p:
        by_album.setdefault(album_id, set()).add(p[:1] == b"/")

print("items.path   ABSOLUTE", len(absolute), "| relative", len(items) - len(absolute))
print("albums.artpath ABSOLUTE", len([a for a in art if a and a[:1] == b"/"]),
      "| relative", len([a for a in art if a and a[:1] != b"/"]),
      "| NULL", len([a for a in art if not a]))
print("mixed_albums", len([1 for forms in by_album.values() if len(forms) > 1]))
print("outside_rows", len(outside), "(correctly absolute, stay as they are)")
print("items_that_will_change", len(absolute) - len(outside))
print("albums_that_will_change",
      len([a for a in art if a and a.startswith(prefix)]))
for p in absolute[:3]:
    print("sample absolute path:", p)
```

```sh
docker compose run --rm musicdrop python /data/repair-census.py
```

It opens the database read-only and imports no beets code, so it cannot trip the migration. Delete
the file from `data/` when you are done. Section 5 gives the same fallback for the two steps that
*write*.

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

Run every **beets** command from inside the container, so that `directory` and the stored paths are
in the same namespace, and so that the beets doing the work is the same version MusicDrop itself
uses — that matters, because a different beets on the host may not carry this migration at all.
`beet` is on `PATH` there (`/app/.venv/bin/beet`, verified in the published image). It needs
`BEETSDIR` set on every invocation, as below: without it `beet` tries to create `/app/.config`,
cannot, and dies with a `PermissionError` before reading anything.

Two path namespaces are in play and the commands below mix them deliberately. `docker compose`
lines and plain `sqlite3` lines run on the **host**, where the database is `./data/beets/library.db`
(from the `./data:/data` volume). Anything inside `sh -c` runs in the **container**, where the same
file is `/data/beets/library.db` and the music is `/music`. Adjust the host side to wherever your
compose file lives. The `sqlite3` lines are host-only by necessity, not by preference — the image
has no `sqlite3` CLI (section 2); if your host has none either, use the Python fallbacks below,
which do exactly the same three writes from inside the container.

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

**Between step 4 and step 6, run no `beet` command at all.** *Opening* the library is what fires
the migration, and every `beet` subcommand opens it — measured: a bare `beet --version` repaired a
lab library outright, banner and backups and all. This is also why section 4's checks belong before
step 4 and not after: step B's `beet ls` is safe only while the ledger row is still in place. The
`sqlite3` and `python` commands in steps 2, 4 and 5 never import beets, so none of them can trip
it.

Steps 1, 3, 6 and 8 are `docker compose` wrappers around the operation that was rehearsed; the
rehearsal ran `beet ls` directly with `BEETSDIR` set. See section 10 for exactly which parts were
executed and which were not.

Step 4 deletes two rows — `relative_path|items` and `relative_path|albums`. The migration covers
`path` on items **and** `artpath` on albums, so one run fixes cover-art paths too. That is why
this is better than a hand-written `UPDATE items`.

**Python fallbacks for steps 2, 4 and 5,** if the host has no `sqlite3`. These run in the
container and touch no beets code, so they cannot trip the migration early:

```sh
# 2. Your own backup (the SQLite backup API — the same one beets uses for its own .bak files).
docker compose run --rm musicdrop python -c 'import datetime, sqlite3; src = sqlite3.connect("/data/beets/library.db"); dest = "/data/beets/library.db.pre-path-repair-" + datetime.date.today().isoformat(); dst = sqlite3.connect(dest); src.backup(dst); dst.close(); src.close(); print("backup written to", dest)'

# 4 + 5. Clear the ledger and report what is left, in one command (expect: deleted 2 ... remaining: 0).
docker compose run --rm musicdrop python -c 'import sqlite3; db = sqlite3.connect("/data/beets/library.db"); n = db.execute("DELETE FROM migrations WHERE name = ?", ("relative_path",)).rowcount; db.commit(); left = db.execute("SELECT count(*) FROM migrations WHERE name = ?", ("relative_path",)).fetchone()[0]; db.close(); print("deleted", n, "ledger rows; remaining:", left)'
```

`deleted 0` means step 4 changed nothing: either the ledger row was never there, or you are
pointed at a different database. Do not go on to step 6 until it says `deleted 2`.

Expected output from step 6, in this order:

```
Created database backup at: '/data/beets/library.db-before-items-relative_path.bak'.
Migrating path for 25576 items...
Migration complete: 22100 of 25576 items updated
Created database backup at: '/data/beets/library.db-before-albums-relative_path.bak'.
Migrating artpath for N albums...
Migration complete: M of N albums updated
```

That is code order — each backup is written **before** its own table is touched, and prints there
too. You get this order because the image sets `PYTHONUNBUFFERED=1`. The backup line comes from a
plain `print()` while the `Migrating` lines are written straight to `sys.stdout.buffer` and flushed
on the spot, so with buffering on and stdout piped — a bare `beet` run on the host, which is how
the earlier rehearsal was done — the two backup lines land at the *end* instead. Both orderings
were measured. Same work, same guarantee, different flush order: if you see the backup lines last,
nothing is wrong.

**A table's `Migrating`/`Migration complete` pair appears only if that table holds at least
one absolute row.** A table with nothing to convert prints its backup line and nothing else —
`_migrate_field` returns before printing, while `migrate_model` still takes the backup and
still records the ledger row. The 2026-08-15 production run looked exactly like that: the
items half printed all three lines, the albums half printed only its backup line, because
every surviving `artpath` was already relative (21 relative, 3,098 NULL, 0 absolute). Judge
each table against its own census count from section 2, not against this transcript.

If you see **no `Migrating` lines**, read the backup lines to tell the two very different causes
apart:

- **No `Migrating` lines and no backup lines either** — the migration was skipped entirely, because
  the ledger row is still there. Step 4 did not take effect (wrong database file is the usual
  reason). Nothing ran and nothing is burned: fix the path, redo steps 4 and 5, and run step 6
  again.
- **No `Migrating` lines but both backup lines present** — the migration *did* run, found no
  absolute row to fix, and re-recorded itself.
  `beets/library/migrations.py` `_migrate_field` returns before printing anything when no row is
  absolute, while `migrate_model` still takes both backups and still calls `record_migration` —
  which is exactly why the backup lines are the tell and the missing banner is not. **Judge it
  per table, against the section 2 census.** For a table the census counted absolute rows in,
  a missing banner means **the one shot is burned** (recover per section 7): you are pointed at
  the wrong `library.db`, or that database was already repaired. For a table the census found
  clean, backup-line-only is the correct, healthy outcome — on the 2026-08-15 production run
  the albums half printed exactly that, because its artpaths were already relative, while the
  items half showed the full three-line pair. Nothing was burned.

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

`mixed_albums` reaches 0 only if none of those `k` outside rows shares an album with rows inside
the music directory; such an album stays "mixed" for the rest of its life and that is correct, not
a failed repair. Measured in a lab built that way on purpose: one outside row in a four-track
album left `mixed_albums 1` after a repair that was otherwise complete. If `outside_rows` is 0,
`mixed_albums` must be 0.

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

The values must **not** start with `X'2F`. Without `sqlite3` on the host, the same check reads
better anyway — these must **not** start with a slash:

```sh
docker compose run --rm musicdrop python -c 'import sqlite3; db = sqlite3.connect("file:/data/beets/library.db?mode=ro", uri=True); db.text_factory = bytes; print(*[r[0] for r in db.execute("SELECT path FROM items ORDER BY id DESC LIMIT 3")], sep="\n")'
```

---

## 7. If it went wrong: recovering the burned shot

If the census shows nothing changed, the migration ran against a `directory` that did not match
your rows. It has recorded itself and will not run again on its own. Nothing is damaged. Recover
by clearing the ledger and retrying with the right `directory`:

```sh
sqlite3 ./data/beets/library.db "DELETE FROM migrations WHERE name = 'relative_path';"
# fix `directory` so it resolves to the prefix from section 4 step A, then reopen the library
```

(Same command, same caveat as step 4: this one needs `sqlite3` on the host. Without it, use the
combined clear-and-confirm fallback from section 5.)

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

Then re-run in the **published image** (`ghcr.io/elienop/musicdrop:latest`) through real
`docker compose run --rm`, against smaller throwaway libraries whose rows carry the container-side
`/music` prefix:

- step 6 verbatim, including the `grep` pipe — and its output ordering three ways: in the image as
  shipped, in the image with `PYTHONUNBUFFERED` unset, and as a bare host `beet` with stdout piped.
  The first gives code order; the other two put the backup lines last
- all three step-6 outcomes: ledger cleared with absolute rows (full banner), ledger cleared with
  no absolute row (backup lines only, ledger re-recorded — the burned shot), ledger intact
  (silence, no `.bak` written)
- `beet` **is** on `PATH` in the image — `/app/.venv/bin/beet`, beets 2.13.1 on Python 3.11.15;
  the earlier "inferred, not observed" caveat is retired. It does need `BEETSDIR` set: without it
  `beet` cannot write `/app/.config` and dies with a `PermissionError` before doing anything
- the image has **no `sqlite3` CLI**, and every Python fallback in sections 2, 5 and 6, whose
  numbers were cross-checked against the host `sqlite3` CLI on the same database and matched
  exactly (including `outside_rows` and the two dry-run counts)
- the missing-file loop with the `docker compose run` prefix
- `docker compose run --rm` works despite `container_name:` being set — Compose names the one-off
  container separately
- section 4 steps A, B and C verbatim against a lab in the production state (absolute rows, ledger
  already recorded): step B's `beet ls` fires nothing there, and writes no `.bak`
- and the reverse, which is why the warning above exists: with the ledger cleared, `beet --version`
  and step B's `beet ls` each ran the whole migration on their own
- section 7's throwaway-config escape hatch in the exact shape printed there (`directory: /music`,
  a host `library:` path, `plugins: []`), run as a bare host `beet`: it repaired a library whose
  rows carried `/music/…` on a machine with no `/music` at all

**Still not executed:** `docker compose stop`, `pull`, and `up -d` against a real deployment.
They are ordinary wrappers, but nobody has run this sequence end to end on the production host.

Check the `beet` invocation prints the `Migrating path for …` lines before trusting any wrapper.

The production numbers (22,100 / 3,476 / 123) were measured on 2026-08-15 by running the section 2
census on the TrueNAS box itself. Not by the rehearsal, and not by the investigation of the same
day — that one never had access to the production library. Treat them as a snapshot of that day
and re-run the census before trusting any of them.
