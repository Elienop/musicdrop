import { AlertTriangle, Pencil } from "lucide-react";
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

export function AlbumEditPanel({ album, onClose }: { album: AlbumDetail; onClose: () => void }) {
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
        <Button variant="ghost" onClick={onClose} disabled={applying}>Cancel</Button>
      </div>
    </section>
  );
}

/** The full before -> after diff for a pending edit: album-header fields, the
 * per-track changes, and a move warning when files will be relocated. */
function PreviewDiff({ preview }: { preview: AlbumEditPreview }) {
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
        <p className="text-muted-foreground">No changes — the album already matches your edits.</p>
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
        <MoveNotice count={preview.move_plan.length} />
      )}
    </section>
  );
}

/** Every changed track, current -> proposed (track #, title, artist). Mirrors
 * the import review's TrackDiff (# / Now / After columns). */
function TrackDiffTable({ tracks }: { tracks: EditTrackChange[] }) {
  return (
    <div className="flex flex-col gap-2">
      <h4 className="text-sm font-medium">Tracks · {tracks.length}</h4>
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
                {[t.title_before, t.artist_before].filter(Boolean).join(" · ") || "—"}
              </TableCell>
              <TableCell>
                <span className="flex min-w-0 items-center gap-2">
                  <span className="truncate font-medium">
                    {[t.title_after, t.artist_after].filter(Boolean).join(" · ") || "—"}
                  </span>
                  <Pencil className="text-muted-foreground size-3 shrink-0" aria-label="changed" />
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
 * --warning token (matches the Replace action elsewhere). */
function MoveNotice({ count }: { count: number }) {
  return (
    <div
      role="alert"
      className="border-warning/50 bg-warning/10 text-warning flex items-start gap-2 rounded-md border p-3 text-sm"
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>
        {count} file{count === 1 ? "" : "s"} will be moved on disk to match the new tags —
        this relocates the files in your library.
      </span>
    </div>
  );
}

/** The per-item outcome of an apply: counts + any failed tracks (never hidden). */
function ApplyOutcome({
  result,
}: {
  result: components["schemas"]["AlbumEditResult"];
}) {
  const wrote = result.items.filter((i) => i.written).length;
  const moved = result.items.filter((i) => i.moved).length;
  const failures = result.items.filter((i) => Boolean(i.error));

  return (
    <div className="flex flex-col gap-2 text-sm">
      <p role="status">
        Updated · wrote {wrote} tag{wrote === 1 ? "" : "s"}
        {moved > 0 && ` · moved ${moved} file${moved === 1 ? "" : "s"}`}
      </p>
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
                {failureLabel(i)} — {i.error}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function failureLabel(item: ItemWriteResult): string {
  const num = item.track === null || item.track === undefined ? null : `#${item.track}`;
  const parts = [num, item.title].filter(Boolean);
  return parts.length ? parts.join(" ") : `Item ${item.item_id}`;
}

/** A single before/after track-number cell (e.g. "1" or "1 → 2"). */
function trackCell(before: number | null | undefined, after: number | null | undefined): string {
  const b = before ?? null;
  const a = after ?? null;
  if (a !== null && a !== b) return b === null ? String(a) : `${b} → ${a}`;
  return String(b ?? a ?? "–");
}

function diffValue(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}

function Field({ id, label, value, onChange, disabled, inputMode }: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  inputMode?: "numeric";
}) {
  return (
    <div className={cn("flex flex-col gap-1", disabled && "opacity-60")}>
      <label htmlFor={id} className="text-sm font-medium">{label}</label>
      <Input id={id} value={value} disabled={disabled} inputMode={inputMode}
        onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
