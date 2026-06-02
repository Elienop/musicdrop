import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { usePreviewAlbumEdit, useApplyAlbumEdit } from "@/api/useAlbumEdit";
import type { components } from "@/api/schema";
import type { AlbumDetail } from "@/api/useAlbum";

type AlbumEditRequest = components["schemas"]["AlbumEditRequest"];
type AlbumEditPreview = components["schemas"]["AlbumEditPreview"];

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

  const onPreview = () => {
    const req = buildRequest(album, draft);
    previewMutation.mutate(req, { onSuccess: setPreview });
  };
  const onApply = () => {
    const req = buildRequest(album, draft);
    applyMutation.mutate(req, { onSuccess: () => setPreview(null) });
  };

  return (
    <section aria-label="Edit album" className="flex flex-col gap-4 rounded-lg border p-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field id="edit-album-artist" label="Album artist"
          value={draft.album_artist}
          onChange={(v) => setDraft((d) => ({ ...d, album_artist: v }))} />
        <Field id="edit-album-title" label="Album title"
          value={draft.title} onChange={(v) => setDraft((d) => ({ ...d, title: v }))} />
        <Field id="edit-album-year" label="Year"
          value={draft.year} onChange={(v) => setDraft((d) => ({ ...d, year: v }))} />
        <Field id="edit-album-genre" label="Genre"
          value={draft.genre} onChange={(v) => setDraft((d) => ({ ...d, genre: v }))} />
      </div>

      <table className="w-full text-sm">
        <thead>
          <tr className="text-muted-foreground text-left">
            <th className="w-16">#</th><th>Title</th><th>Artist</th>
          </tr>
        </thead>
        <tbody>
          {album.tracks.map((t) => {
            const td = draft.tracks[t.id];
            const set = (k: "title" | "track" | "artist", v: string) =>
              setDraft((d) => ({ ...d, tracks: { ...d.tracks, [t.id]: { ...d.tracks[t.id], [k]: v } } }));
            return (
              <tr key={t.id}>
                <td><Input aria-label={`track number ${t.id}`} value={td.track}
                  onChange={(e) => set("track", e.target.value)} className="w-14" /></td>
                <td><Input aria-label={`track title ${t.id}`} value={td.title}
                  onChange={(e) => set("title", e.target.value)} /></td>
                <td><Input aria-label={`track artist ${t.id}`} value={td.artist}
                  onChange={(e) => set("artist", e.target.value)} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {preview && (
        <div className="rounded-md border p-3 text-sm">
          {preview.changed_fields.length > 0 && (
            <p>Will change: {preview.changed_fields.join(", ")}</p>
          )}
          {preview.album_after.title !== undefined && preview.changed_fields.includes("title") && (
            <p className="font-medium">{preview.album_after.title}</p>
          )}
          {preview.move_enabled && preview.move_plan.length > 0 && (
            <p className="text-amber-600">
              Will move {preview.move_plan.length} file{preview.move_plan.length === 1 ? "" : "s"} to match the new tags.
            </p>
          )}
        </div>
      )}

      {applyMutation.isError && (
        <p className="text-destructive text-sm" role="alert">{applyMutation.error.message}</p>
      )}
      {applyMutation.isSuccess && (
        <p className="text-sm" role="status">
          Updated · wrote {applyMutation.data.items.filter((i) => i.written).length} tags
          {applyMutation.data.move_failures > 0 && ` · ${applyMutation.data.move_failures} move(s) failed`}
        </p>
      )}

      <div className="flex gap-2">
        <Button variant="secondary" onClick={onPreview} disabled={previewMutation.isPending}>
          Preview
        </Button>
        <Button onClick={onApply} disabled={!preview || applyMutation.isPending}>
          Apply
        </Button>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
      </div>
    </section>
  );
}

function Field({ id, label, value, onChange }: {
  id: string; label: string; value: string; onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">{label}</label>
      <Input id={id} value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
