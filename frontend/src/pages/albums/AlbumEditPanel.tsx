import { SectionLabel } from "@/components/system/SectionLabel";
import { Edit as EditIcon, Warning } from "@/components/icons";
import { useState } from "react";

import { useApplyAlbumEdit, usePreviewAlbumEdit } from "@/api/useAlbumEdit";
import type { AlbumDetail } from "@/api/useAlbum";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

type AlbumEditRequest = components["schemas"]["AlbumEditRequest"];
type AlbumEditPreview = components["schemas"]["AlbumEditPreview"];
type EditTrackChange = components["schemas"]["EditTrackChange"];
type ItemWriteResult = components["schemas"]["ItemWriteResult"];
type TrackMoveRefusal = components["schemas"]["TrackMoveRefusal"];

/** Human label for an album-header field in the diff (vs the raw beets key). */
const FIELD_LABEL: Record<string, string> = {
  album_artist: "Album artist",
  title: "Album title",
  year: "Year",
  genre: "Genre",
};

function buildRequest(album: AlbumDetail, draft: Draft): AlbumEditRequest {
  const albumEdits: NonNullable<AlbumEditRequest["album"]> = {};
  if (draft.album_artist !== album.album_artist) albumEdits.album_artist = draft.album_artist;
  if (draft.title !== album.title) albumEdits.title = draft.title;
  const yearNum = draft.year === "" ? null : Number(draft.year);
  if (yearNum !== album.year) albumEdits.year = yearNum;
  if (draft.genre !== (album.genre ?? "")) albumEdits.genre = draft.genre;

  const tracks = album.tracks.flatMap((t) => {
    const td = draft.tracks[t.id];
    // A live refetch (SSE library:changed) can change this album's track
    // membership while the panel is open, surfacing an id the once-seeded draft
    // has no entry for. Such a track carries no user edits to send, so skip it
    // rather than dereference an undefined draft entry (which throws inside the
    // Preview/Apply click handler and silently no-ops the request).
    if (!td) return [];
    const changes: NonNullable<AlbumEditRequest["tracks"]>[number] = { item_id: t.id };
    let changed = false;
    if (td.title !== t.title) { changes.title = td.title; changed = true; }
    const trackNum = td.track === "" ? null : Number(td.track);
    if (trackNum !== t.track) { changes.track = trackNum; changed = true; }
    if (td.artist !== t.artist) { changes.artist = td.artist; changed = true; }
    return changed ? [changes] : [];
  });

  return { album: Object.keys(albumEdits).length ? albumEdits : null, tracks };
}

type Draft = {
  album_artist: string;
  title: string;
  year: string;
  genre: string;
  tracks: Record<number, { title: string; track: string; artist: string }>;
};

function initialDraft(album: AlbumDetail): Draft {
  return {
    album_artist: album.album_artist,
    title: album.title,
    year: album.year === null ? "" : String(album.year),
    genre: album.genre ?? "",
    tracks: Object.fromEntries(
      album.tracks.map((t) => [t.id, { title: t.title, track: String(t.track), artist: t.artist }]),
    ),
  };
}

export function AlbumEditPanel({ album, onClose }: Readonly<{ album: AlbumDetail; onClose: () => void }>) {
  const [draft, setDraft] = useState<Draft>(() => initialDraft(album));
  const [preview, setPreview] = useState<AlbumEditPreview | null>(null);
  const previewMutation = usePreviewAlbumEdit(album.id);
  const applyMutation = useApplyAlbumEdit(album.id);

  const applying = applyMutation.isPending;
  // While applying, every control is frozen so the in-flight request always
  // matches the diff the user confirmed.
  const inputsDisabled = applying;

  // Any edit invalidates a shown preview: the diff must always describe exactly
  // what Apply will send, so a stale preview is cleared and Apply re-gated.
  const editDraft = (next: (d: Draft) => Draft) => {
    setDraft(next);
    setPreview(null);
    // A new edit invalidates the last apply: drop its success/failure banner so
    // a stale "Updated · wrote N tags" outcome can't linger over fresh edits.
    applyMutation.reset();
  };

  const onPreview = () => {
    previewMutation.mutate(buildRequest(album, draft), { onSuccess: setPreview });
  };
  const onApply = () => {
    applyMutation.mutate(buildRequest(album, draft), {
      onSuccess: (result) => {
        // Reseed the draft from the refreshed album so the panel reflects what
        // was actually written; clear the now-spent preview.
        setDraft(initialDraft(result.album));
        setPreview(null);
      },
    });
  };

  return (
    <section aria-label="Edit album" className="flex flex-col gap-4 rounded-lg border p-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field id="edit-album-artist" label="Album artist" disabled={inputsDisabled}
          value={draft.album_artist}
          onChange={(v) => editDraft((d) => ({ ...d, album_artist: v }))} />
        <Field id="edit-album-title" label="Album title" disabled={inputsDisabled}
          value={draft.title} onChange={(v) => editDraft((d) => ({ ...d, title: v }))} />
        <Field id="edit-album-year" label="Year" inputMode="numeric" disabled={inputsDisabled}
          value={draft.year} onChange={(v) => editDraft((d) => ({ ...d, year: v }))} />
        <Field id="edit-album-genre" label="Genre" disabled={inputsDisabled}
          value={draft.genre} onChange={(v) => editDraft((d) => ({ ...d, genre: v }))} />
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-16">#</TableHead>
            <TableHead>Title</TableHead>
            <TableHead>Artist</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {album.tracks.map((t, i) => {
            const td = draft.tracks[t.id];
            // A live refetch (SSE library:changed) can change this album's track
            // membership while the panel is open, surfacing an id the once-seeded
            // draft has no entry for. Skip that row until the draft reseeds rather
            // than dereference an undefined draft entry (which crashes the page).
            if (!td) return null;
            const set = (k: "title" | "track" | "artist", v: string) =>
              editDraft((d) => ({ ...d, tracks: { ...d.tracks, [t.id]: { ...d.tracks[t.id], [k]: v } } }));
            // Label by the track's position/title, not the beets item id.
            const ref = td.title || `track ${i + 1}`;
            return (
              <TableRow key={t.id} className="hover:bg-transparent">
                <TableCell>
                  <Input aria-label={`track number for ${ref}`} inputMode="numeric" value={td.track}
                    disabled={inputsDisabled}
                    onChange={(e) => set("track", e.target.value)} className="w-14" />
                </TableCell>
                <TableCell>
                  <Input aria-label={`title of track ${i + 1}`} value={td.title}
                    disabled={inputsDisabled}
                    onChange={(e) => set("title", e.target.value)} />
                </TableCell>
                <TableCell>
                  <Input aria-label={`artist of track ${i + 1}`} value={td.artist}
                    disabled={inputsDisabled}
                    onChange={(e) => set("artist", e.target.value)} />
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>

      {preview && <PreviewDiff preview={preview} />}

      {applyMutation.isError && (
        <p className="text-destructive text-sm" role="alert">{applyMutation.error.message}</p>
      )}
      {applyMutation.isSuccess && <ApplyOutcome result={applyMutation.data} />}

      <div className="flex gap-2">
        <Button variant="secondary" onClick={onPreview}
          disabled={previewMutation.isPending || applying}>
          Preview
        </Button>
        <Button onClick={onApply} disabled={!preview || applying}>
          Apply
        </Button>
        <Button variant="ghost" onClick={onClose} disabled={applying}>
          {/* After a successful apply the edit is already written — "Cancel"
              would wrongly imply it reverts. Show "Done" until the next edit
              (which resets the mutation) gives something to cancel. */}
          {applyMutation.isSuccess ? "Done" : "Cancel"}
        </Button>
      </div>
    </section>
  );
}

/** The full before -> after diff for a pending edit: album-header fields, the
 * per-track changes, a move warning when files will be relocated, and the
 * renames the apply will refuse because their destination is already taken. */
function PreviewDiff({ preview }: Readonly<{ preview: AlbumEditPreview }>) {
  const before = preview.album_before;
  const after = preview.album_after;
  const fieldRows = preview.changed_fields.map((f) => ({
    label: FIELD_LABEL[f] ?? f,
    before: diffValue(before[f as keyof typeof before]),
    after: diffValue(after[f as keyof typeof after]),
  }));
  const hasChanges = fieldRows.length > 0 || preview.tracks.length > 0;

  return (
    <section
      aria-label="Pending changes"
      className="flex flex-col gap-4 rounded-md border p-3 text-sm"
    >
      {!hasChanges && (
        <p className="text-muted-foreground">No changes; the album already matches your edits.</p>
      )}

      {fieldRows.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-32">Field</TableHead>
              <TableHead>Now</TableHead>
              <TableHead>After</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {fieldRows.map((row) => (
              <TableRow key={row.label} className="hover:bg-transparent">
                <TableCell className="text-muted-foreground">{row.label}</TableCell>
                <TableCell className="text-muted-foreground">{row.before}</TableCell>
                <TableCell className="font-medium">{row.after}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {preview.tracks.length > 0 && <TrackDiffTable tracks={preview.tracks} />}

      {preview.move_enabled && preview.move_plan.length > 0 && (
        <MoveNotice
          count={preview.move_plan.length}
          refused={preview.move_refusals.length}
        />
      )}

      {preview.move_refusals.length > 0 && (
        <MoveRefusalList refusals={preview.move_refusals} />
      )}
    </section>
  );
}

/** Every changed track, current -> proposed (track #, title, artist). Mirrors
 * the import review's TrackDiff (# / Now / After columns). */
function TrackDiffTable({ tracks }: Readonly<{ tracks: EditTrackChange[] }>) {
  return (
    <div className="flex flex-col gap-2">
      <SectionLabel>Tracks · {tracks.length}</SectionLabel>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-12 pr-4 text-right">#</TableHead>
            <TableHead>Now</TableHead>
            <TableHead>After</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {tracks.map((t) => (
            <TableRow key={t.item_id} className="bg-primary/5 hover:bg-transparent">
              <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
                {trackCell(t.track_before, t.track_after)}
              </TableCell>
              <TableCell className="text-muted-foreground">
                {[t.title_before, t.artist_before].filter(Boolean).join(" · ") || "-"}
              </TableCell>
              <TableCell>
                <span className="flex min-w-0 items-center gap-2">
                  <span className="truncate font-medium">
                    {[t.title_after, t.artist_after].filter(Boolean).join(" · ") || "-"}
                  </span>
                  <EditIcon className="text-muted-foreground size-3 shrink-0" aria-hidden="true" />
                </span>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

/** A warning that files will be relocated on disk, styled with the project's
 * --warning token (matches the Replace action elsewhere).
 *
 * `count` is the move plan, which the backend keeps free of refused renames, so
 * the two counts partition the pending moves and this headline can never read a
 * refusal as a file that will move. */
function MoveNotice({ count, refused }: Readonly<{ count: number; refused: number }>) {
  return (
    <div
      role="alert"
      className="border-warning/50 bg-warning/10 text-foreground flex items-start gap-2 rounded-md border p-3 text-sm"
    >
      <Warning className="text-warning mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>
        {count} file{count === 1 ? "" : "s"} will be moved on disk to match the new tags;
        this relocates the files in your library.
        {refused > 0 && (
          <>
            {" "}
            <span className="text-destructive font-medium">
              {refused} other file{refused === 1 ? "" : "s"} cannot be moved.
            </span>
          </>
        )}
      </span>
    </div>
  );
}

/** The renames the apply will REFUSE: each computed destination is already taken,
 * so moving would rename a file nobody asked to rename. The backend keeps these
 * out of `move_plan`, so a refusal is never also a pending move — hence the
 * destructive treatment of reorganize's ConflictList rather than MoveNotice's
 * warning one, and the same vocabulary ("cannot be moved"), since both features
 * refuse for the same reason.
 *
 * No `role="alert"`: that is for results arriving unbidden, and this sits inside
 * a preview the user just opened. Apply stays enabled either way — the tags
 * still write, and the apply reports this same sentence on the track's own row.
 *
 * DIVERGES from ConflictList in exactly one place, deliberately: the intro says
 * what apply still does. An edit applies PARTIALLY — the tags write and only the
 * rename is refused — so without that sentence a safe edit reads as dangerous,
 * and the "N move failures" banner afterwards reads as fresh bad news rather
 * than the outcome just predicted. Reorganize refuses whole units and does
 * nothing to them, so its ConflictList must NOT carry this sentence. */
function MoveRefusalList({ refusals }: Readonly<{ refusals: TrackMoveRefusal[] }>) {
  return (
    <div className="flex flex-col gap-1">
      <p className="text-muted-foreground text-xs">
        Cannot be moved (the destination name is already taken):{" "}
        <span className="text-destructive font-medium">{refusals.length}</span>. Their tags will
        still be updated; these files just keep their current names.
      </p>
      <ul
        aria-label="Files that cannot be moved"
        className="text-destructive max-h-56 overflow-auto rounded-md border px-3 text-sm"
      >
        {refusals.map((r) => (
          <li key={r.item_id} className="flex flex-col gap-0.5 border-b py-1.5 last:border-b-0">
            <span className="font-medium">{refusalLabel(r)}</span>
            {/* `detail` names the contested destination itself, so it reads alone. */}
            <span className="text-muted-foreground text-xs">{r.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Which track, and the rename that cannot happen. Basenames only: both paths are
 * absolute, while `detail` below already spells the destination out relative to
 * the library. */
function refusalLabel(r: TrackMoveRefusal): string {
  const num = trackTag(r.track);
  const rename = renameSummary(r.old_path, r.new_path);
  return num === null ? rename : `${num} ${rename}`;
}

/** The shortest pair of path tails that still shows a difference. An album-title
 * or artist edit relocates the DIRECTORY, leaving every basename untouched, so
 * basenames alone would render the headline as a rename to itself. */
function renameSummary(oldPath: string, newPath: string): string {
  if (baseName(oldPath) !== baseName(newPath)) {
    return `${baseName(oldPath)} → ${baseName(newPath)}`;
  }
  return `${parentAndName(oldPath)} → ${parentAndName(newPath)}`;
}

function baseName(p: string): string {
  const i = p.lastIndexOf("/");
  return i === -1 ? p : p.slice(i + 1);
}

/** `parent/name`; the whole path when it has no parent segment to drop. */
function parentAndName(p: string): string {
  const i = p.lastIndexOf("/");
  if (i === -1) return p;
  return p.slice(p.lastIndexOf("/", i - 1) + 1);
}

/** `#N`, or null when there is no track number to show. beets reports a file with
 * no track number as 0, so the sentinel is suppressed the same way an absent one
 * is — otherwise one untracked file reads "#0 Title" here and bare there. */
function trackTag(track: number | null | undefined): string | null {
  return track === null || track === undefined || track === 0 ? null : `#${track}`;
}

/** The per-item outcome of an apply: counts + any failed tracks (never hidden). */
function ApplyOutcome({
  result,
}: Readonly<{
  result: components["schemas"]["AlbumEditResult"];
}> ) {
  const wrote = result.items.filter((i) => i.written).length;
  const moved = result.items.filter((i) => i.moved).length;
  const reexported = result.playlists_reexported;
  const failures = result.items.filter((i) => Boolean(i.error));

  // Zero counts are dropped, so "Updated" stands alone when an apply had
  // nothing of a given kind to do — "wrote 0 tags" reads as a failure. Built as
  // a list rather than inline separators: with three counts the "put a · here
  // only if something precedes AND something follows" conditions multiply.
  const counts: string[] = [];
  if (wrote > 0) counts.push(`wrote ${wrote} tag${wrote === 1 ? "" : "s"}`);
  if (moved > 0) counts.push(`moved ${moved} file${moved === 1 ? "" : "s"}`);
  if (reexported > 0) {
    counts.push(
      `re-exported ${reexported} playlist${reexported === 1 ? "" : "s"}`,
    );
  }

  return (
    <div className="flex flex-col gap-2 text-sm">
      <output className="block">
        Updated
        {counts.length > 0 && ` · ${counts.join(" · ")}`}
      </output>
      {(result.write_failures > 0 || result.move_failures > 0) && (
        <div
          role="alert"
          className="border-destructive/40 bg-destructive/5 flex flex-col gap-1 rounded-md border p-3"
        >
          <p className="text-destructive font-medium">
            {result.write_failures > 0 && `${result.write_failures} write failure${result.write_failures === 1 ? "" : "s"}`}
            {result.write_failures > 0 && result.move_failures > 0 && " · "}
            {result.move_failures > 0 && `${result.move_failures} move failure${result.move_failures === 1 ? "" : "s"}`}
          </p>
          <ul className="flex flex-col gap-0.5">
            {failures.map((i) => (
              <li key={i.item_id} className="text-muted-foreground">
                {failureLabel(i)}: {i.error}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function failureLabel(item: ItemWriteResult): string {
  const parts = [trackTag(item.track), item.title].filter(Boolean);
  return parts.length ? parts.join(" ") : `Item ${item.item_id}`;
}

/** A single before/after track-number cell (e.g. "1" or "1 → 2"). */
function trackCell(before: number | null = null, after: number | null = null): string {
  if (after !== null && after !== before) {
    return before === null ? String(after) : `${before} → ${after}`;
  }
  return String(before ?? after ?? "-");
}

function diffValue(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "-";
  return String(v);
}

function Field({ id, label, value, onChange, disabled, inputMode }: Readonly<{
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  inputMode?: "numeric";
}> ) {
  return (
    <div className={cn("flex flex-col gap-1", disabled && "opacity-60")}>
      <label htmlFor={id} className="text-sm font-medium">{label}</label>
      <Input id={id} value={value} disabled={disabled} inputMode={inputMode}
        onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
