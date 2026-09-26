# MusicDrop

**The eye on your music library** — see what you have (and what you're missing), drive your tagging and enrichment, and build Plex-ready playlists, all without touching a terminal.

MusicDrop is a from-scratch rebuild. It keeps the name, logo, and product vision of the original (now archived for reference), but inverts the architecture: rather than maintaining its own metadata matcher, MusicDrop puts a comprehensive **web UI on top of [beets](https://beets.io)** — the mature, ~15-year-old music tagger — and adds what beets lacks: a rich visual library, playlists, and multi-user Plex sync.

## What it does

- **See your library.** Browse artists, albums, and tracks; spot the gaps (what you have vs. don't); search, and view cover art, lyrics, and track info. The library as a visual surface, not a file explorer.
- **A UI for beets.** beets' *toggles* (its config) and *actions* (`import`, `modify`, `fetchart`, `duplicates`, …) surfaced as real pages — do everything you'd do at the `beet` CLI, in the browser. The interactive import/match step gets a proper candidate-picker.
- **Playlists.** Create, edit, and delete them easily; import them from `.m3u` files or straight from Plex; **Plex-compatible**, multi-user.
- **Acquisition.** slskd downloads land in a watched inbox and flow through a unified **Review** page into the same beets import pipeline (deemix is a future adapter on the same seam).

## Architecture — the layers

beets owns the **engine and the library** (its `library.db` is the source of truth for matching, tagging, organizing). MusicDrop is the **web face and value-add** around it:

| Layer | Owner |
|---|---|
| Acquisition (slskd; deemix planned) | MusicDrop |
| Match · enrich · organize · library | **beets** |
| Decision (auto-accept policy + human review UI) | MusicDrop |
| Presentation (browse · search · art / lyrics / info) | MusicDrop |
| Playlists · Plex sync · multi-user | MusicDrop |

beets and MusicDrop are co-located on the same host: beets' library (`library.db`) and config (`config.yaml`) live on a shared path; beets' actions run in-process through a typed adapter (`app/beets/`).

## Stack

- **Backend** — Python ≥ 3.11, **FastAPI + Pydantic** on Uvicorn; **beets 2.14** (pinned `beets==2.14.*`) runs in-process behind the typed adapter in `app/beets/`. HTTP via httpx, Plex via [python-plexapi](https://github.com/pkkid/python-plexapi), YAML config editing via ruamel.yaml. Packaged with **uv**; `mypy --strict`, **Ruff** (lint + format), and **pytest** enforced in CI.
- **Frontend** — **React 19 + TypeScript**, built with **Vite**. UI is **shadcn/ui** (Radix primitives) + **Tailwind CSS 4** + [Phosphor](https://phosphoricons.com/) icons, with **League Spartan** as the display face; server state via **TanStack Query**; routing via React Router. The API client is **openapi-fetch**, and the TypeScript API types are **generated** from the backend's OpenAPI schema — never hand-written. Tested with Vitest + Testing Library + MSW.

## Status

**Actively built.** A deep beets integration and the web UI are in place.

**Shipped**

- **Browse** — artist → albums → tracklist, with cover art, lyrics, and a release's missing tracks.
- **Search** across the library.
- **Cover art** — fetch + replace.
- **Artist images** — portraits resolve automatically from the configured sources (fanart.tv → Spotify → Deezer, first verified match wins; Deezer needs no key) and are written into the library for Plex. To change one, open an artist and use the image action: pick a source, **Fetch**, and **Use this image** to keep it — or upload a file / paste a URL. **Reset to auto** asks first, then forgets both your pick and the cached automatic image, so the artist is looked up again from scratch; an image you uploaded or pasted moves to the Trash rather than being deleted, into an entry named after the artist, and if the Trash cannot be used the reset stops — check the Trash before retrying, because a move that failed part-way left the image there. An artist whose portrait isn't cached yet shows their initials while it resolves in the background; it appears without a reload when it lands.
- **Artist art for Plex** — turn the **Settings → Artist art for Plex** toggle on and MusicDrop
  writes `artist-poster.*` and `artist-background.*` into each of an artist's folders, the names
  Plex's Local Media Assets reads. The library-wide backfill in that panel fills gaps only. An
  artist page's **Save art to library** button replaces what is there: the poster/background it
  overwrites go to the Trash first, one entry per artist folder. To get a file you placed by hand
  back, copy it out of `data/beets/trash/` — the Trash page's Restore cannot re-file loose images.
  If the Trash cannot be used, that folder is reported failed and its files are left alone. A
  folder reported failed *after* its files moved has them in its Trash entry — look there before
  you empty the Trash.
  Art is written only into folders reached from the library root without following a symlink: an
  artist or album folder that *is* a link, or that sits under one, is reported failed with a log
  line naming it, and nothing is written or moved aside. The library root itself may be a link.
  To spread a library across disks, bind-mount the second disk into the library instead of
  linking to it.
  Renaming or merging an artist writes art only where it is missing — a merge never replaces the
  target's art.
- **Lyrics** — presence, per-album fetch, and a library-wide backfill. The backfill is
  **fill-gaps-only on disk**: it writes a `.lrc`/`.txt` sidecar only where none exists and
  never deletes or replaces one you already have. The one exception is a sidecar whose
  entire content is the legacy `[Instrumental]` marker: an instrumental verdict cleans it
  up, and a found verdict treats it as absent and replaces it with the fetched lyrics — a
  real sidecar, including one half of a mixed pair, is never deleted or replaced.
  Sidecars follow the same rule as artist art: a track whose album folder is only reachable
  through a symlink below the library root is skipped with a log line, and a bind mount is the
  supported way to put part of a library on another disk.
- **Edit tags** — album & track, from the UI. A rename that would land the album's cover on a name another file already holds is refused before anything moves — the tag changes still write, the files stay put, and the preview says why (beets alone would silently rename the cover to a `.1` sibling).
- **Import** — interactive candidate picker, resume, an import-time duplicate guard (duplicates always route to review, whatever `duplicate_action` says — and when the match you apply is already in your library, the duplicate question opens next, on the same album), and search-by-release-ID when the right match isn't offered. **Stop this run** ends a folder import you started, at the album it is on: what already landed stays, and adding the folder again asks about the rest. Unattended runs, slskd's included, **bank** every album they cannot finish for later review instead of stalling, and the summary verifies each album actually **landed** in the library. A start whose source folder is not there, or cannot be read, is refused up front with the reason — it no longer runs to a hollow *Import finished — 0 albums imported*. So are your library or a folder that holds it, MusicDrop's own folders, and slskd's whole download folder or a folder that holds it; an album inside the library or inside slskd's folder still imports. A banked row whose folder stopped answering says so and names the fix; **Rescan folder** clears it once the folder reads again.
- **Keep downloads** (Settings → Beets) — on, imports hardlink each file into the library and the download stays; off, they move. The switch writes beets' own `import.hardlink` / `import.move` and reloads beets. A hardlinked file is one file with two names, so tag writes change the download too (beets' `import.write: no` stops that, and your library's tagging with it). It needs one filesystem: see **One mount for music and downloads**.
- **Duplicates** — find & resolve duplicate albums (resolve one, or resolve-all).
- **Release identity** — which release an album is (source · label · country · media · disambiguation), with view-release links.
- **Delete & Trash** — delete albums or artists into a Trash; restore or empty it under
  **Settings → Trash**. A delete moves the album's tracks, its cover and its lyric files; any
  other file in the folder stays where it is, and the folder stays while something is left in
  it (a file your beets `clutter:` list names goes with the emptied folder, as in any beets
  move). During the move MusicDrop puts a temporary `.musicdrop-keep` file in its own folders
  above the album (inbox, Trash, playlists) so beets does not remove them, then deletes it; one
  left behind by a crash is harmless and safe to delete. Three setup faults refuse a delete with a 503, and none of them
  drops a library row. The first is the music root being missing, empty or unreadable (an unmounted
  share), so a genuinely emptied library needs a remount (or beets' own CLI) before its
  leftover entries can be cleared. It is checked before the delete starts and again per album,
  so nothing has moved when it refuses — with one narrow exception, a share that drops during
  an album's own move, which keeps that album's rows and can leave part of its folder under the
  Trash container. The second is the origin-records folder — `<beets dir>/trash-origins/`,
  described below — being unusable: not there and not creatable, something other than a folder
  in its place, or not readable or writable by the container's user (a wrong `PUID`/`PGID`, a
  restored backup, a read-only `/data`). That one is asked ahead of every branch of a delete,
  so nothing has moved when it refuses. MusicDrop will not delete an album it cannot record the
  origin of, because the name it would hand that folder in Trash may still be spoken for by a
  record it cannot see. The third is the Trash folder itself not being reachable, creatable or
  checkable — see `MUSICDROP_TRASH_DIR` below. The message says which of the three it is; fix
  that folder's permissions or its mount and retry. Two rules cover the folders on the way to the
  Trash, and they bite when it ends up inside the music library: one of those folders must NAME
  the library root — either of its spellings, `directory:` itself or a link to it — or the path
  is refused; and none of them may be a link *into* the library. The app checks that one itself:
  it resolves every link in the path rather than letting the kernel follow it, and refuses the
  link whose target lands inside the library. The message names the path it refused, and — when
  the part in the way sits inside a link the app resolved rather than in the path you typed —
  that link and where it points. A target that dips into the library and climbs back out with
  `..` is refused too, since the folder it climbs into is the library root's parent: spell it
  without the detour. Links pointing anywhere else are followed as before, so a Trash on another
  disk through a link of yours still works. One blind spot stays: a library folder bind-mounted
  to an outside path reads as outside. `..` in the setting itself is refused outright — so spell
  the Trash through `directory:`'s own root, without `..`. A bind mount *inside* the library is
  fine.
  **Deleting an album** additionally covers the case a stray file used
  to hide: a `.stfolder`, a `lost+found` or an empty leftover directory sitting on a local
  mountpoint whose share has dropped makes the folder look mounted, so before a delete drops
  rows having moved nothing, MusicDrop confirms that at least one of the music *files* the
  library names is really on disk. It checks the file rather than the folder holding it because
  a naming template with no folder in it (**Settings → Naming**, beets' own `$title`) puts
  every track straight in the music root — and asking whether the music root exists is the
  question the stray file already answered wrongly. The cost of the stricter test is that an
  album whose folder survives with its sampled track removed by hand no longer counts as
  present. At any realistic library size that changes nothing — with 200 albums and one track,
  five tracks or a whole folder missing it refused 0 times in 1000 draws, because one album
  whose file really is there is enough. It bites where *every* album it samples is in that
  state: a single-album library whose one track was removed by hand, or a share whose folders
  are all present and whose files are not (measured: refused every time, at 1, 2, 6, 20 and 200
  albums). Between those it is a rate rather than a shape — with half of 200 albums missing
  their sampled file, 33 refusals in 1000 draws. **Restore** runs that same stronger check, above both of the ways it can
  put a folder back — it writes *into* the music library, so an unmounted share is the same
  catastrophe there, and a restore that hits one is refused with a 503 having moved nothing out
  of Trash. Disk sync is the deliberate exception: it keeps the cheap "is the root there" test,
  because it runs it once per removal and accepted the same residual for itself. Either fault
  appearing part-way through an artist delete — the share dropping, or the origin-records
  folder becoming unusable — is reported rather than refused, because the 503's promise is no
  longer true by then: the answer says how many of the artist's albums were moved to Trash
  before it stopped — those are recoverable there, and the albums it never reached are
  untouched. A run that moved nothing does not claim a count it cannot back: it says how many
  albums it dropped that had no files left to move, and its advice is to *check* the Trash
  folder rather than a promise that anything is in it. That hedge is deliberate, and it now
  covers a narrower set: a move that stops PART-WAY — a copy across filesystems that fails
  between the copy and the delete, or a track that cannot be moved after others already were —
  can leave some of it under Trash without this end being able to see it, so the honest
  instruction is to look. **If removing the library rows fails after the files reached Trash,
  the album stays listed and its files stay in Trash.** Delete the album again: the retry only
  drops the rows. Until then **Empty refuses that entry**, because it is the album's only copy.
- **Restore knows where things came from.** When MusicDrop moves a folder to Trash it
  records where that folder came from in a small JSON file alongside — one per Trash entry,
  under `<beets dir>/trash-origins/`, deliberately outside the trashed folder and outside
  your music library — so each row in **Settings → Trash** says what Restore will do before
  you click:
  - **Exact restore** — the folder goes straight back to its own path. This is the only way
    back for an art/booklet leftover the reorganize sweep collected, which has no audio and
    therefore cannot be re-imported at all.
  - **Approximate restore** — no usable origin, so beets re-imports the folder and files it
    under your *current* naming rules rather than putting it back. The row says which of the
    three reasons applies: there is no record MusicDrop can use (it may predate origin
    records, its record may have failed to write, or that record may be unusable now — the
    server log says which), its files were moved to Trash one by one — which is every album
    you delete — or its origin is no longer inside the library. Restore stays available in all
    three; the row just tells you it will not be exact. Restoring brings back the album's
    tracks; what else the entry holds is what the entry lists.
  - **Files moved aside** — loose files MusicDrop moved out of the way when it replaced them:
    art a **Save art to library** run overwrote, or a portrait **Reset to auto** removed. They are
    not an album, so there is no Restore button — copy them out of the entry into the folder the row
    names. Empty works as usual. A Trash on a different disk from the library is fine: the files are
    copied across and keep their permissions and timestamp, and a link is put back as a link.
  - **Can’t be restored** — the fifth way a row loses its exact move-back, and the only one
    whose Empty is turned down too: the Trash entry is itself a *link* to a folder elsewhere
    (usually on another volume, which is how an album whose own folder is a link gets here,
    but it can point anywhere — at a sibling Trash entry, at a file), so acting on it would
    act on whatever it points at rather than on the row you picked. In the ordinary case the
    album's own files were never moved — they are still where the link points, and adding
    that folder through Import is what puts it back in the library. Both of the row's own
    buttons are disabled, because as of this version MusicDrop turns both down on the link
    itself, without ever following it — so Restore and that row's own Empty both answer
    "not in Trash" and change nothing. **Empty all** is the only control that removes such an
    entry, and it removes the link alone. The row is listed whether or not the link's target
    is reachable — an unmounted one used to show nothing at all.

  Two consequences worth knowing. A row trashed by an older version has no record and never
  will, so a media-free one (art/booklet leftovers with no audio) still has Empty as its only
  exit. (If you move or delete a Trash entry outside MusicDrop, its record is left behind.
  For anything MusicDrop itself puts in Trash that is harmless: it treats a recorded name as
  taken, so the next album it trashes under that name simply gets a `(1)` suffix. A store it
  cannot use does not send that name out again either — it refuses the delete instead (the
  503 above). A folder that arrives in Trash by some *other* route — a hand copy, a
  restored backup, a sync client writing into the volume — asks nothing, so it can land on that name and its
  row will then offer an exact restore to the *previous* folder's path. Nothing detects
  that today; if you put folders into the Trash directory by hand, check what the row
  promises before clicking Restore. Removing the entry through the app clears both together
  — except for a link entry, which only **Empty all** can remove, and which has no record to
  clear because MusicDrop never writes one for a link.) And a Restore is refused rather than
  merged when *something is at the album's original path again* — a folder with anything in
  it, a file, or a symlink including a broken one — nothing moves, the files stay in Trash,
  and the row tells you to clear that path first; an empty leftover folder is not in the way
  and gets replaced. A Trash row listed at zero tracks only means MusicDrop couldn't read
  audio tags there; beets' importer reads more formats than the listing does, so Restore may
  still work — except on a link entry, which says outright that it can’t be restored. A link
  to a *folder* lists at zero tracks too, because nothing under it is read; a link a hand has
  placed at a media *file* is listed as that file and shows its tags. The hedge is dropped on
  the refusal, not on the count.
- **beets config** — viewer + writable editor, with advisory notices for import keys MusicDrop forces (a saved value that only affects CLI runs is flagged, not silently accepted). `import.delete` is one of them: MusicDrop forces it off, so a copy-mode import leaves your download where it was; a source already inside the library is moved instead. `duplicate_action: upgrade` saves too; in-app imports ignore it, and a CLI `beet import` that upgrades deletes the replaced files outright, not to Trash. Validate and Save read your text the way beets does, and refuse most of what would stop beets starting and bad `import:` switches (`musicbrainz: no`, a bad `replace:` pattern, an include beets would skip, `write: 1`), in beets' own words. Save writes the text exactly as you typed it, comments and `yes`/`no` included. Any plugin name saves; beets decides. **Apply** changes nothing and says what to fix when `config.yaml` is missing, has a YAML error (with its line), lists an `include:` beets would skip (and why), or sets a store layout MusicDrop refuses; if beets rejects a value while loading, Apply puts the running config back. MusicDrop won't start with a skipped include, or with more than 32 includes or 1 MiB of them. Saving a symlinked `config.yaml` writes the file it points to: the link stays, that file keeps its mode, and its folder must be writable. The read-only view hides every setting beets marks secret, whether or not its plugin is on; a copy made with a YAML anchor is not hidden.
- **Naming** — edit beets path/replace rules with a live preview. **Reorganize** — re-apply them to existing files, and sweep emptied leftover folders into the Trash — the sweep offers only folders the walk found no audio beneath (it does not look inside dot-folders or through symlinks), and skips a live album's own art folder. A move that would silently rename an album's cover (a stray file already holds the cover's name at the destination) is refused instead: the preview flags it as an art conflict, and apply holds back just that album.
- **Disk sync** — a `beet update` equivalent: preview-first removal of library entries whose files were deleted outside the app, plus tag refresh for files changed on disk.
- **Library dashboard** — counts, duration, size, recently added.
- **Playlists** — create / edit / delete; import from uploaded `.m3u`/`.m3u8` files or straight from Plex, with a preview that matches every entry against the library before committing (entries it can't match stay as pending rows to resolve later); merge one playlist into another; per-playlist cover artwork (upload / replace / remove — a Plex-sourced import seeds its poster automatically); `.m3u8` export, kept fresh automatically — when a library operation moves or removes files (tag edits, Reorganize, deletes, duplicate resolves, disk sync), affected playlists are re-exported, and operations that show an outcome line (tag edits, bulk duplicate resolves, Reorganize, disk sync) report how many; Plex-compatible, multi-user sync (metadata-matched, with cascade-delete). Configure Plex under **Settings → Integrations** (base URL + admin token + the music-library path *as Plex sees it*), or seed it from `MUSICDROP_PLEX_URL` / `MUSICDROP_PLEX_TOKEN` / `MUSICDROP_PLEX_LIBRARY_PATH` / `MUSICDROP_PLEX_LIBRARY_SECTION` (the Plex music-section title; leave it empty only when the server has a single music library — with several, sync refuses until one is named).
- **Acquisition (slskd)** — completed slskd downloads land in a watched inbox (webhook-driven) and queue into the import pipeline; a unified **Review** page is the one home for import decisions and inbox backlog. slskd is configured under **Settings → Sources**, which shows its folder (set with `MUSICDROP_INBOX_DIR`) and marks it **Missing** when auto-import is on and the folder is not there.
- **Add from folder** — type a path or **Browse** to it, or pick one of your Folder sources or Recent folders (the last 10 that started, kept in this browser); a pick only fills the path box. Then **Review now** to decide each album, or **Sweep & bank** to import confident matches and bank the rest for Review.
- **Folder sources** (**Settings → Sources**) — name the folders you import from often; each becomes a button on **Add from folder**. A folder must exist and is refused where an import would refuse it (the library, MusicDrop's own folders, slskd's whole folder). Removing one touches no files.
- **Faceted Browse** — slice the library by genre · decade · format · type · media · country · source · lyrics coverage, sorted A–Z or recently added.
- **Live updates** — library changes stream to every open tab (SSE), no manual refresh.
- **Sign-in** — a single-password login screen, a 30-day session cookie, and a **Sign out** control in the top bar. On a fresh install the same screen is the **setup form** that creates the password; change it later under **Settings → Account** — see [Authentication](#authentication).
- **Dark, art-forward UI** — violet-accented dark theme, dissolving detail rails, Koito-inspired row cards, a two-font type system (League Spartan display face), and the original MusicDrop logo re-colored onto the design tokens.

**Planned** — deemix acquisition adapter.

## Screenshots

| Overview | Artist | Album |
|---|---|---|
| ![Overview — library stats and recently added](docs/screenshots/overview.png) | ![Artist page — portrait rail and album rows](docs/screenshots/artist.png) | ![Album page — cover rail and tracklist](docs/screenshots/album.png) |

## Install (Docker)

MusicDrop ships as a single container: `ghcr.io/elienop/musicdrop` (amd64), FastAPI serving both the API and the UI on port **3030**.

```yaml
services:
  musicdrop:
    image: ghcr.io/elienop/musicdrop:latest
    container_name: musicdrop   # the `docker inspect musicdrop` commands below assume this
    ports:
      - "3030:3030"
    environment:
      - PUID=1000   # match the owner of your music share
      - PGID=1000   # (tag writes / reorganize keep that ownership)
      - TZ=Etc/UTC
      # - MUSICDROP_ARTIST_IMAGE_FANARTTV_API_KEY=…   # optional: enables fanart.tv portraits (see below)
    volumes:
      - ./data:/data            # beets library.db + config.yaml + app state
      - /path/to/media:/media   # one folder: your library in media/music, downloads beside it
    restart: unless-stopped
```

`docker compose up -d`, then open `http://<host>:3030`. First boot writes a starter beets config to `data/beets/config.yaml` with `directory: /media/music`; edit it under **Settings → Beets** (plugins, import behavior) — MusicDrop reads it like the beets CLI would, with one carve-out: in-app imports force a few `import.*` keys (`autotag`, `duplicate_action`, `singletons` — and `incremental`, `incremental_skip_later` and `resume` on sweep, hardlink, bank-apply, inbox and **Import them again** runs) so the review flow stays intact, and force `delete` **off** so an import never removes the files it just filed. `copy`/`move`/`link`/`hardlink`/`reflink` stay yours on every import, slskd's included (**Keep downloads** writes `move` or `hardlink` for you). Under a config that keeps downloads, a slskd folder MusicDrop imported leaves "Not imported yet" until a file is added, removed or renamed in it; to drop a download for good, delete it in slskd (System → Files, which needs slskd's `remote_file_management: true`), except under `link` or in place, where that breaks its album. A hardlink keeps your download, so MusicDrop uses beets' import history on a hardlink run from **Add from folder**: adding a kept folder again skips albums beets has already imported; when that is all a run did, **Import them again** imports them anyway. If an import stops part-way, the album's page names the folder still outside the library. It offers to add that folder again only when the folder holds every track; adding a part-folder sends the already-filed tracks to Trash, and under `move` leaves the album short. A hardlink that cannot cross filesystems stops the second run too, until the operation or the mount changes. An import does not start while the music folder is missing, or empty while the library still lists tracks (the share is unmounted); queued inbox and bank work waits and resumes by itself once it is back. A new install's empty music folder is fine. A folder name the app cannot display is shown with a placeholder and still imports when that shown path is pasted back; two folders that display alike are refused rather than guessed. A folder that is not there is refused at the start, instead of running an import that finds nothing. **Browse folders** beside Add from folder's path box lists the server's folders, opening at the path box's folder (its nearest existing parent if it is gone), else at the newest Recent folder, else at `/media`; it hides what beets' `ignore` and `ignore_hidden` hide, marks the library and MusicDrop's own folders, and still shows a slskd folder "Not imported yet" has hidden. The editor shows an advisory when a saved value won't take effect in-app; a CLI `beet import` still honours it. Optional integrations (slskd webhook, Plex) are configured under Settings or via `MUSICDROP_*` env vars; for slskd, set `MUSICDROP_INBOX_DIR` to its downloads folder inside the same mount (e.g. `/media/downloads/slskd`). **Path in slskd** (`MUSICDROP_SLSKD_DOWNLOADS_PREFIX`, saved as `downloads_prefix`) is that folder as slskd sees it; leave it empty when both containers use the same path. A download outside it, by whole folder names, is not imported: the log names both paths, the slskd card says the last one didn't match, and the folder waits under "Not imported yet". Upgrading: a value that matched only as text (`/app/down` for `/app/downloads`) now refuses, and an empty field with identical paths now works. Re-paste the slskd card's webhook block (**Settings → Sources**) into slskd's config too: the new one has slskd retry a delivery MusicDrop missed, the old one tries once. The fanart.tv/Spotify artist-image credentials are **env-only** — Settings holds just the two on/off toggles: set `MUSICDROP_ARTIST_IMAGE_FANARTTV_API_KEY` to enable fanart.tv (`MUSICDROP_ARTIST_IMAGE_FANARTTV_CLIENT_KEY` is an optional extra passed alongside it), and both `MUSICDROP_ARTIST_IMAGE_SPOTIFY_CLIENT_ID` and `MUSICDROP_ARTIST_IMAGE_SPOTIFY_CLIENT_SECRET` for Spotify; with none set, portraits resolve from Deezer alone (which needs no key).

Upgrading with the old `/music` volume? Your `config.yaml` keeps its `directory:`. Only a fresh `/data` starts at `/media/music`, and imports refuse until that folder is mounted, so change the mount and `directory:` together. An install still on the old starter's `copy: yes` + `move: no` now copies slskd downloads instead of moving them, so they stay and disk use grows; to move them, turn **Keep downloads** on, then off.

### One mount for music and downloads

Mount one folder that holds your library and your downloads, as `/media` above. A hardlink needs both on one filesystem: two ZFS datasets are two filesystems, and two bind mounts fail too, even of one disk. Across them a **Keep downloads** import stops with “Can’t hardlink across filesystems”, and a move becomes a full copy.

A few more knobs are env-only, with defaults that suit most setups:

- `MUSICDROP_INBOX_SETTLE_SECONDS` (default 60) — the quiet window an inbox folder must hold before **Review all** will import it: the inbox is slskd's live output dir, so a folder touched within the last 60 s is skipped (listed as still receiving; a per-row Review overrides) rather than imported half-finished, where the remainder would later re-import as a duplicate.
- `MUSICDROP_MAX_BODY_BYTES` (default 25 MiB) — the request-body cap: anything larger (an oversized cover upload, a giant playlist import) is refused with a 413 before the body is read.
- `MUSICDROP_TRASH_DIR` (default `<beets dir>/trash`) — where deleted albums wait, on the same mount as `directory:` (`/media/music/.trash`). Inside the music library or the beets data dir is fine. Refused, with a 503 at every delete: a Trash that is or contains the music library, the beets data dir, the origin store or another app folder; a `..` anywhere in the path; a folder on the way that is not a real directory once the path is inside the library; a path that reaches into the library without naming its root, including through a link on the way; a path through more than 40 links; and a path the app cannot check at all (a folder on the way it may read but not enter — fix that folder's permissions). A bind mount is the supported way to put the Trash on another disk; how the folders "on the way" are judged, and where that judgement is blind, is under **Delete & Trash** above.
- `MUSICDROP_TRASH_ORIGINS_DIR` (default `<beets dir>/trash-origins`) — the restore records (see **Backup & restore**); needs a folder of its own: not inside the music library or the Trash, and not on top of either; the default `<beets dir>/trash-origins` is fine.
- `MUSICDROP_BEETS_DIR` and beets' `directory:` must be separate trees (the shipped `/data` and `/media/music` are), and `library:` may not sit in the Trash or the origin store — a red row on the offending line in **Settings → Beets**, with Save and Apply refused.
- `MUSICDROP_LYRICS_BACKFILL_DELAY_SECONDS` (default 0.2) — the courtesy inter-track pause during lyrics fetches (the library-wide backfill and per-album fetches), also used as the inter-artist pause in the artist-image backfill; beets separately rate-limits the lyrics HTTP itself.

**Browsing by DNS name?** Requests are only accepted when the `Host` is an IP literal,
`localhost`, or a name listed in `MUSICDROP_ALLOWED_HOSTS` (comma-separated) — a
DNS-rebinding guard, same shape as Plex's and Transmission's. Reaching MusicDrop through a
reverse proxy or any hostname (`http://nas.local:3030`, `https://music.example.com`) requires
listing that name: `MUSICDROP_ALLOWED_HOSTS=music.example.com`. By-IP access always works.
Behind a proxy that rewrites `Host`, the forwarded public name (`X-Forwarded-Host`) must be in the list too — Caddy forwards both by default.
The same goes for server-to-server callers — a proxy that rewrites `Host` to an upstream
*name*, or another container calling MusicDrop by service name (slskd's webhook posting to
`http://musicdrop:3030`) — list those names too; container/host IPs always work.

### Authentication

MusicDrop has a **single account**, protected by one password. Every `/api/*` request — and
`/docs`, `/redoc`, `/openapi.json` — is refused with a `401` unless the browser holds a valid
session cookie. The only exceptions are the container healthcheck (`/api/health`), the slskd
webhook (which carries its own shared secret), and the three sign-in endpoints themselves —
first-run setup, sign-in, and the status check the sign-in screen asks first.

The password guards more than your library. Settings → Beets can enable any beets plugin, so a
signed-in session can run commands as MusicDrop's user: `hook` and `convert` run shell commands,
and `inline` runs Python in the server whenever a path template is evaluated, the Naming preview
included.

**Until a password exists, the only page a visitor can reach is the setup form — and it hands
the account to whoever submits it first.** That is deliberate — there is no "unprotected by
default" mode, and no setup token to present either — so bring the app up behind a firewall (the
LAN it is built for), or set `MUSICDROP_PASSWORD_HASH` (below) before the first boot if the port
is reachable by strangers. On a fresh install the browser lands on that **setup form** instead
of the sign-in form: choose a password, confirm it, and you are signed in. There is no username,
no length rule and no e-mail; an empty (or whitespace-only) password is refused, the same rule
the `hash_password` command applies. What gets stored is a scrypt hash, not the password:
`data/beets/password-hash`, mode `0600`, written atomically beside `session-secret`. The
startup log says which state the server is in, on one line — its `auth:` clause reads
`NO password configured` until setup runs, points at the setup form, and names
`MUSICDROP_PASSWORD_HASH` as the override (below); once a password exists it says where the
password came from, the file or the environment.

With a password set, MusicDrop opens on a **sign-in screen** — one password field, no username, and
it is the only page an unauthenticated visitor can reach. Signing in sets a cookie that lasts
**30 days**, survives container restarts, and is `HttpOnly` + `SameSite=Lax`; you land on
whichever page you originally asked for rather than being dropped on the Overview. If the server
refuses, it says why in the form itself — a wrong password, a hash it cannot read, a sign-in
already in flight — rather than failing blankly.

**Sign out** is in the top bar, beside the activity indicator, at every window width; it clears
the cookie in that browser.

**Changing the password** is under **Settings → Account**: current password, new password,
confirm. A wrong current password is refused and nothing changes. Saving rewrites
`password-hash`, keeps the browser you did it from signed in, and signs every other browser out
(see below).

**Forgot it?** There is no reset link, deliberately — nothing to e-mail and nothing to guess.
Remove the hash file and restart MusicDrop; the sign-in screen is the setup form again. The same
recovery covers a hash MusicDrop cannot read: whatever is at that path counts as a password that
is present, and if it cannot be read as a hash, sign-in is refused and the setup form stays hidden
until it is fixed or removed. Two ways to do the same thing, depending on which side of the bind
mount you are standing on:

```bash
# through the container:
docker exec musicdrop rm /data/beets/password-hash && docker compose restart musicdrop
# or on the host side of the ./data bind mount:
rm ./data/beets/password-hash && docker compose restart musicdrop
```

A directory at that path needs `rm -r`; plain `rm` stops at a directory.

Restart as well, for two reasons: the startup line reports the password source at boot and
not again, and a container start is also when the entrypoint re-owns everything under `/data`
(what a root `docker exec` leaves behind there is recorded as unverified in `BACKLOG.md`).
Setting the new password writes a new hash, which signs every other browser out — the paragraph
after next says why. Deleting that file *is* the reset, so it is as protected as the data
directory it lives in, which already holds the library. Both forms write a line to the container
log when they store a password, in the same stream as uvicorn's own startup lines — the setup
form at `WARNING` and **Settings → Account** at `INFO`, each naming the file — so if the setup
form was ever used by someone who was not you, the log says so.

#### Overriding the password from the environment

`MUSICDROP_PASSWORD_HASH` is the other way to set the password, and the older one. A non-empty
value there **wins over the file** and hides both the setup form and the change-password form —
**Settings → Account** shows a notice explaining the override instead. It stays for two reasons:
as a recovery lever (regain access without deleting anything, then decide), and for operators
who would rather keep the credential in their deployment config — which also keeps it outside
the data volume. With the file as the source, losing the data directory loses the password with
it (a bind mount that comes up empty, a restore that misses the file), and the instance is
claimable again through the setup form until someone runs it; the override is the way to keep
the credential somewhere the data volume cannot take it. The two sources do not merge:
nothing copies the env var into the file, and a file written earlier is shadowed while the
override is set — unset it and restart, and that file is the password again (or, with no file,
the setup form is back).

Generate the hash first — **never put the plaintext password in an env var**, which is why
MusicDrop has no setting for one:

```bash
docker exec -it musicdrop python -m app.auth.hash_password
# or, from a checkout:  cd backend && uv run python -m app.auth.hash_password
```

It prompts twice (hidden — nothing reaches your shell history) and prints a self-describing
scrypt string like `scrypt$131072$8$1$vIfqnSPZ…$bTMGckBk…`, followed by the same value with
every `$` doubled, labelled as the compose form. Put that one in your compose file and restart:

```yaml
    environment:
      # Every $ DOUBLED: compose reads a $ before a letter or _ as a variable reference.
      - MUSICDROP_PASSWORD_HASH=scrypt$$131072$$8$$1$$vIfqnSPZ…$$bTMGckBk…
```

The doubling is only a `docker-compose.yml` quirk. In an `.env` file, an `env_file:`, or a plain
`docker run -e`, paste the value exactly as printed.

**The override wins even when its value is unreadable.** A hash that compose has mangled is
still a set override. With the `$` left single, compose treats a `$` followed by a letter or
underscore as a variable reference and swallows the name, up to the first character that is not
a letter, digit or underscore; a `$` before a digit is kept, so `$131072$8$1` and the base64 `=`
padding survive, and whichever of the salt and digest start with a letter lose their leading run.
Measured with Compose 5.5.0 on a hash the generator printed:
`scrypt$131072$8$1$5SpsLX…sA==$xLM5OQ…PsE=` arrived as `scrypt$131072$8$1$5SpsLX…sA===` — the
digit-led salt intact, the letter-led digest reduced to its `=` — and `docker compose config` had
warned that *the "xLM5OQ…PsE" variable is not set*. Once in a while both fields happen to start
with a digit or a symbol, the raw value arrives intact and works, and the trap is hidden rather
than removed; usually what arrives no longer parses — the incident this feature came out of —
and then sign-in is refused, the setup form stays hidden, and the refusal names the `$$` rule —
as does the startup line, which reads `MUSICDROP_PASSWORD_HASH is set but UNREADABLE`. That is
a deliberate asymmetry with the Plex and slskd settings, where a saved file beats its seed env
var: a typo in the override falling through to the setup form would create a second password
that the corrected env var later shadows, and a lockout lever that can be shadowed is not one.
Fix the value or remove it; the refusal message says the same.

**Changing the password signs every other session out, everywhere.** The cookie is signed with a
key derived from the password hash — the file's or the environment's, whichever is in effect —
so a new hash invalidates every outstanding cookie. The change-password form re-issues the cookie
of the browser that submitted it and no other; a new `MUSICDROP_PASSWORD_HASH` plus a restart
re-issues none, so every browser signs in again. Which is what you want if a password ever leaks,
and worth knowing before you rotate one casually. Deleting `data/beets/session-secret` and
restarting does the same thing without changing the password.

The one thing you cannot do is revoke a *single* session early: **Sign out** expires the cookie
in the browser, but the token itself stays valid until it expires or until you rotate one of the
two values above. There is no server-side session list, deliberately — nothing to store, sweep
or back up.

**The `Secure` flag follows the connection, so a TLS proxy pays for itself.** If you sign in over
HTTPS — directly, or through a reverse proxy that forwards `X-Forwarded-Proto` — the cookie is
marked `Secure` and your browser will refuse to send it over plain HTTP from then on.

Behind a proxy this decision rests entirely on that header, so it is worth knowing what your proxy
does with it. **Caddy** is safe by default: `reverse_proxy` sets `X-Forwarded-Proto`, and its
documentation is explicit that "for these `X-Forwarded-*` headers, by default, the proxy will
ignore their values from incoming requests, to prevent spoofing". The exception is
[`trusted_proxies`](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) — configure it
and Caddy starts *trusting* incoming `X-Forwarded-*` values from those ranges, so a range wide
enough to cover ordinary clients hands them the ability to set their own. **nginx** adds no
`X-Forwarded-*` header at all by default (its only default `proxy_set_header` directives are `Host`
and `Connection`), so a proxied deployment there needs
`proxy_set_header X-Forwarded-Proto $scheme;` — and setting it with `$scheme` rather than passing
the client's value through is what keeps it trustworthy.

The failure mode worth avoiding: a proxy that *forwards* a client-supplied `X-Forwarded-Proto`
instead of overwriting it lets a client claim `http` on a TLS connection and be handed a cookie
without `Secure`. Check your own proxy rather than assuming.

**The honest caveat is what remains over plain HTTP.** MusicDrop is designed to be browsed by LAN
IP (`http://192.168.1.10:3030`), and a `Secure` cookie is never sent over plain HTTP — marking it
there would make the app impossible to sign into in its primary deployment, so on that path the
flag is deliberately absent and the session cookie is only as private as your network. That is an
accepted residual, not an oversight. If you expose MusicDrop beyond your LAN, put it behind a
reverse proxy terminating TLS (which encrypts the hop that matters, *and* hardens the cookie as
above) and keep it off the open internet.

Releases are automatic: every merged PR that touches anything beyond markdown/docs publishes a new image tag (`vX.Y.Z`, plus `latest`) with generated notes on the [Releases page](https://github.com/Elienop/musicdrop/releases); a docs-only merge cuts no release of its own and ships with the next one.

**One-time repair, for libraries built before the path fix.** Every release up to and including v0.34.1 stored MusicDrop-imported track paths in `library.db` *absolutely* (`/music/Artist/…`) instead of relative to the music directory, the way beets does. Nothing is damaged, but such rows do not follow the music share if it ever moves to a new mount, dataset, or machine. The release containing `fix(import): store item paths relative to the music dir` stops it recurring; the existing rows need a separate one-time database repair, and **the two have to land in the same maintenance window** — deploying either half on its own leaves the importer's duplicate lookup worse off than doing neither. The procedure, starting with the census that tells you whether you are affected, is [`docs/import-path-repair.md`](docs/import-path-repair.md).

## Backup & restore

MusicDrop has no built-in backup, deliberately: its state is plain files under the paths you already mount, so a **filesystem snapshot** (ZFS/btrfs on the NAS) is the supported mechanism — nothing to export, and restoring is putting the files back.

**What to snapshot**

| Host path | Mount | Holds |
|---|---|---|
| `./data` | `/data` | `beets/` — library DB, config, bank, playlists, settings, Trash and the Trash origin records — plus `cache/artist-images/` and `cache/cover-thumbs/` |
| your music and downloads | `/media` | the audio files, their embedded tags, `cover.<ext>`, `.lrc`/`.txt` lyric sidecars, `artist-poster.*` / `artist-background.*` |
| slskd downloads *(acquisition only)* | `/media/downloads/slskd` | `.musicdrop-ledger.json` — which slskd folders MusicDrop imported, set aside or failed; without it, kept downloads show under "Not imported yet" again and a repeated webhook re-imports them |

`/media/music` is the library; `/data` is every decision you have made about it. Snapshot both; the host paths above are `docker-compose.yml`'s placeholders. Keep `/data` on a local disk, not a network share: `library.db` and `bank.db` are SQLite files, and SQLite is not safe over a network filesystem.

**Authoritative** — losing it loses work, and nothing regenerates it:

- `data/beets/library.db` — the beets library: every match, tag and organize decision, plus the `lyrics_checked` and `lyrics_instrumental` flags. A `library.db-before-*.bak` sibling is a beets pre-migration copy, the only way back to the previous schema; having none is normal.
- `data/beets/config.yaml` — **Settings → Beets** and **Settings → Naming** both write this file in place and keep no previous copy. If it is a link, back up the file it points to.
- `data/beets/bank/bank.db` — albums banked for review. Pending decisions, not a cache. Versions before it kept one `*.json` file per album there; the first start after the upgrade imports them into `bank.db` once and leaves them untouched. Delete them only once that start logs `bank: imported N rows (0 skipped)`: a skipped row's file is its only copy.
- `data/beets/playlists/*.json` and `data/beets/playlists/artwork/` — MusicDrop owns playlists; Plex is a push target, not a copy.
- `<music>/.playlists/*.m3u8` — the Plex-readable exports. Rewritten only when a playlist changes, never rebuilt wholesale, so the music tree's restore is what covers them; `MUSICDROP_PLAYLISTS_EXPORT_DIR` takes them out of it — snapshot that path too.
- `data/beets/plex/plex.json`, `data/beets/slskd/slskd.json` — the Plex and slskd integration settings, mode `0600`. Not just tokens: Plex's library path/section, slskd's Path in slskd (`downloads_prefix`) and its `auto_import` toggle (lose that and unattended import reverts to its env default, off).
- `data/beets/sources.json` — the Folder sources from **Settings → Sources**: each one's name and folder. Lost, the list is empty; nothing on disk changes.
- `data/beets/password-hash` — the single account's password, as a scrypt hash, mode `0600`, written by the setup form and by **Settings → Account**. Absent, the sign-in screen is the setup form again (that is also the forgotten-password recovery, so a backup without it costs a re-setup, not the library); present but unreadable — whatever sits at that path, if MusicDrop cannot read it as a hash — sign-in is refused and the setup form stays hidden until it is fixed or removed. While `MUSICDROP_PASSWORD_HASH` is set the override wins and this file is shadowed, whatever it holds.
- `<inbox>/.musicdrop-ledger.json` — the record of slskd folders MusicDrop imported, set aside or failed. Defaults to `<beets_dir>/inbox`, inside `/data`; `MUSICDROP_INBOX_DIR` moves it onto the slskd downloads mount — the table's third row.
- `data/beets/trash/` — deleted albums, any artist art a **Save art to library** run replaced, and portraits **Reset to auto** removed, live here and nowhere else until you empty the Trash; normally the only GB-scale item under `data/`.
- `data/beets/trash-origins/*.json` — where each trashed folder came from, one tiny file per Trash entry (two entry names long enough to share a shortened key share one file; the loser falls back to the approximate restore). Nothing else records it: restore the Trash without these and every row falls back to the approximate restore, which for an art/booklet leftover with no audio means no way back at all. `MUSICDROP_TRASH_ORIGINS_DIR` moves them. A backup that leaves this folder ABSENT is fine — the next delete creates it, exactly as a fresh install does. One restored with permissions the container's user cannot read or write is not: deletes are refused with a 503 until it is fixed (see **Delete & Trash** above).
- `data/beets/state.pickle` — beets' import state. The banking sweep's forced `incremental` reads its `taghistory`; without it the next sweep re-offers every folder it has already handled.
- `data/cache/artist-images/` — the `*.override` (+ `*.override.mime`) images you uploaded or pasted by hand, which nothing refetches (a **Reset to auto** moves the pair to Trash, into an entry named after the artist; copy what is in the entry back here to undo it), and `_enabled.json` / `_art_write_enabled.json`, the two artist-image toggles: lose those and both revert to their env defaults (`MUSICDROP_ARTIST_IMAGES_ENABLED` / `MUSICDROP_ARTIST_ART_WRITE_ENABLED`, off unless set).

Those are the shipped image's paths (`MUSICDROP_BEETS_DIR=/data/beets`, `MUSICDROP_ARTIST_IMAGE_CACHE_DIR=/data/cache/artist-images`). Override either and the tree under it moves; a `MUSICDROP_*_DIR` for trash, trash origins, bank, playlists, Plex, slskd or the inbox moves that subtree out from under `<beets_dir>`; and `config.yaml`'s `library:` and `directory:` relocate the DB and the music tree with no env var at all. Snapshot what these resolve to, not the defaults.

**Regenerable** — don't worry about these:

- `data/cache/artist-images/*.bin` · `*.mime` · `*.miss` — the auto-fetch cache and its negative markers, refetched on demand.
- `data/cache/artist-images/*.thumb.bin` · `*.thumb.src` — the 320px WebP portraits the grids and rosters render, re-derived from the `*.override` or `*.bin` beside them the moment the sidecar's source tag stops matching.
- `data/cache/cover-thumbs/` — the same derivation for album covers (`MUSICDROP_COVER_THUMB_CACHE_DIR=/data/cache/cover-thumbs` in the shipped image). Nothing authoritative is here at all: unlike the artist cache this one never owns an original — every cover it thumbnails lives in the music tree, as an art file or an embedded tag. Delete the whole directory and the next page view rebuilds what it needs.
- `data/beets/session-secret` — the key your session cookie is signed with, mode `0600`. Restoring it keeps you signed in across the restore; losing it just means signing in again on the next visit, which is also how you revoke every outstanding session on purpose. It is *not* your password — that is `password-hash` above, or `MUSICDROP_PASSWORD_HASH` — so a snapshot with no `session-secret` still lets you in.
- import, backfill and sweep jobs — in memory only; they don't survive a restart anyway.

**Snapshot consistency**

You need not stop MusicDrop to take a snapshot. `library.db` is SQLite in its default rollback-journal mode (`journal_mode=delete`; neither beets nor MusicDrop switches it to WAL), so a `library.db-journal` sidecar exists only while a write transaction is open, and a snapshot atomic within the dataset captures the DB and that journal together — what SQLite needs to roll the interrupted transaction back. A snapshot of a running MusicDrop is crash-consistent; at worst one in-flight write is discarded.

Separate datasets don't change that, so long as ONE snapshot operation covers both, and [`zfs-snapshot(8)`](https://openzfs.github.io/openzfs-docs/man/master/8/zfs-snapshot.8.html) promises a shared instant for exactly one form — `-r`: "[r]ecursive snapshots created through the `-r` option are all created at the same time". Take it over a common ancestor; within a pool one always exists (`zfs list` shows your layout), and on TrueNAS it is one Periodic Snapshot Task with **Recursive** ticked. Two *separate* operations — different pools, or a task each — are two instants, and moves fall through the gap: deleting to Trash moves an album's files from `/media/music` into `data/beets/trash/` under `/data`, and inbox drops import with your beets file operation, a move unless your config keeps downloads. Caught between the instants, that album is in both snapshots, in neither, or split across them — and `shutil.move` across filesystems is copy-then-delete, so a file can be captured truncated. The result is a folder to re-import or re-delete, not a damaged library; on that layout, snapshot with the container stopped, or at least never during a delete, a Trash restore, or an inbox import.

Quiescence comes from the process being gone, not from the shutdown grace: MusicDrop waits ~5s for an in-flight import to release the slot, but the beets worker is a daemon thread it cannot join, so past that bound the library closes under a still-running import. For the snapshot you keep as the restore point of record, snapshot after `docker compose down` returns.

**Record the image tag in the snapshot's name.** Nothing inside the snapshot records it, and by restore time the container that could tell you is gone — so read it now, from the sidebar's health row, `GET /api/version` (signed in — it is behind the session gate, unlike the bare `/api/health` liveness probe), or `docker inspect musicdrop --format '{{range .Config.Env}}{{println .}}{{end}}' | grep MUSICDROP_VERSION`.

**Never run without the `./data` bind mount**

The image's `VOLUME /data` makes a *missing* bind mount silent rather than fatal: Docker creates an anonymous volume under `/var/lib/docker/volumes/` and your library lives on a path no snapshot policy is aimed at. To confirm where it resolves:

```bash
docker inspect musicdrop --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

If that prints `/var/lib/docker/volumes/<hash>/_data -> /data`, your library is in an anonymous volume. Move it out: `docker compose down`, `mkdir -p ./data && sudo cp -a /var/lib/docker/volumes/<hash>/_data/. ./data/`, add `- ./data:/data` to the compose file, `docker compose up -d`, then re-run the inspect and confirm the app still shows your library. Leave the old volume alone — an orphaned volume costs disk, and it is your only second copy until the next snapshot runs.

**Restoring**

Restore through your NAS's snapshot tooling; a few principles are all this needs. Stop MusicDrop first — it holds the library DB open while it runs. Put back **only** the tree you actually lost: a snapshot laid over a tree you still have reverts everything changed in it since — on the music share every tag write and every cover, portrait and lyric file, and under `./data` every import, edit and decision. Restore files rather than `zfs rollback`, which discards all data changed since the snapshot across the whole dataset ([`zfs-rollback(8)`](https://openzfs.github.io/openzfs-docs/man/master/8/zfs-rollback.8.html)). Pin the image to the tag in the snapshot's name: beets migrates `library.db` on open, one-way, and an old snapshot booted once under a newer image cannot go back without the `library.db-before-*.bak` beets writes before migrating. The bank moves one way too: an older image shows the bank as it was at the upgrade (empty if its old `*.json` files were deleted), and nothing banked, decided or deleted there carries over when you upgrade again. To restore a pre-upgrade snapshot of the bank's `*.json` rows, remove `bank.db` first so they import again. Start the app only once everything is back in place.

**Restoring the music share onto a different path** — a renamed dataset, a new bind mount, a new box — needs [`docs/import-path-repair.md`](docs/import-path-repair.md) done first, on libraries that predate the path fix. Rows holding absolute paths do not follow the move, and the next `beet update` or disk sync removes them from the library: the audio files stay on disk, but their added dates, play counts, lyrics, flexible fields and album grouping do not. The repair is one migration and takes one maintenance window.

**Test the restore once**

An untested restore is a hypothesis. Do the drill once, while nothing is broken: restore a snapshot into a scratch directory and point a throwaway compose file at it — a different port, a different `container_name` (`docker-compose.yml` pins `musicdrop`, so a copy changing only ports and volumes clashes on the name), and the music share **read-only** (`- /path/to/media:/media:ro`) so the drill cannot touch it. If **Library → Overview** shows your library, the backup works. Never point a second container at the live `/data`: MusicDrop must be the only process holding the library open.

## Development

MusicDrop is two apps: a FastAPI backend (`backend/`) and a Vite + React frontend (`frontend/`). Run both in dev — the frontend proxies `/api` to the backend, so there's no CORS or base-URL juggling. Layout: `backend/app/` is the API (`api/` routers · `models/` the Pydantic contract · `beets/` the beets-adapter boundary) with tests in `backend/tests/`; `frontend/` is the React + shadcn UI.

**Prerequisites:** Python ≥ 3.11 with [uv](https://docs.astral.sh/uv/); Node 22+ with npm.

**Backend** (from `backend/`):

```bash
uv sync --extra dev                                # install (incl. dev tools)
uv run uvicorn app.main:app --port 3030 --reload   # API on http://localhost:3030
uv run pytest                                      # tests
uv run mypy                                         # strict typecheck (must be clean)
uv run ruff check                                  # lint
uv run ruff format                                 # format
```

**Frontend** (from `frontend/`):

```bash
npm install        # install
npm run dev        # Vite dev server on http://localhost:5173 (proxies /api -> :3030)
npm run test       # vitest
npm run lint       # eslint
npm run typecheck  # tsc
npm run build      # production build
```

`npm run lint` is deliberately narrow: it enables one rule per SonarQube family this
project has already driven to zero, so it guards against regrowth rather than imposing a
new style. Adding a rule for anything else is a design change — see the header of
`frontend/eslint.config.js`, and `frontend/eslint.config.test.ts`, which proves every
enabled rule still fires.

Keep the backend on port **3030** — that's the target of the Vite dev proxy.

**API types are generated, not hand-written.** The frontend's TypeScript API types come from the backend's OpenAPI schema — never edit `src/api/schema.d.ts` by hand. After changing a Pydantic model, run both steps from the repo root (a CI guard fails if `frontend/openapi.json` drifts from the live spec):

```bash
cd backend && uv run python scripts/dump_openapi.py   # app -> frontend/openapi.json
cd ../frontend && npm run gen:api                     # frontend/openapi.json -> src/api/schema.d.ts
```

**Coverage** (from the repo root):

```bash
make coverage     # both suites with coverage on -> backend/coverage/, frontend/coverage/
```

It writes four gitignored reports for SonarQube: Cobertura XML and JUnit XML for the backend, `lcov.info` and Sonar's generic test-execution XML for the frontend. Coverage is opt-in and off every default path — plain `uv run pytest` and `npm run test` behave exactly as they always have and write nothing; run `npm run test:coverage` for the frontend on its own. (`sonar-scan`, which runs `make coverage` before each upload, is a local wrapper of the maintainer's and is not part of this repo.)

## Decisions

- **Backend: Python-native** (FastAPI + Pydantic, beets driven in-process behind a typed adapter in `app/beets/`). Locked — one runtime, since deemix is Python and slskd is just an HTTP API.
- **The API is the contract.** Every endpoint takes/returns Pydantic models; the OpenAPI schema is the source of truth, and the frontend TypeScript types are generated from it (never hand-written).
- **beets owns the engine + library.** All beets access lives behind the typed adapter in `app/beets/`, where beets' global config/plugin singletons stay isolated.
- **Browse is a single artist spine** (roster → artist → albums → tracks), not a flat album grid.
