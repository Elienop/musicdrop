import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";

import {
  type ImportEntryPreview,
  type PlaylistImportFile,
  type PlaylistImportPreview,
  type PlaylistImportPreviewResponse,
  type PlaylistImportRequest,
  useImportCommit,
  useImportPreview,
  usePlexImportPlaylists,
} from "@/api/usePlaylistImport";
import { Back, Playlists, Search, Upload } from "@/components/icons";
import { type PickedTrack, TrackMatchPicker } from "@/components/playlists/TrackMatchPicker";
import { ErrorState } from "@/components/system/ErrorState";
import { PageHeader } from "@/components/system/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDuration } from "@/lib/format";

/** Per playlist (keyed by INDEX in the preview — names aren't unique) → per
 * entry position → the chosen library `item_id` (or `null` when the entry
 * stays unmatched and will commit as a pending record). */
type Resolutions = Map<number, Map<number, number | null>>;

/** Composite key for the picker-sourced label store (playlist index + position). */
function pickedKey(playlistIndex: number, position: number): string {
  return `${playlistIndex}:${position}`;
}

/** Seed resolutions from a fresh preview: matched entries adopt their match,
 * everything else starts unresolved (null) for the user to fix. Keyed by the
 * playlist's index so two same-named playlists in one preview stay distinct. */
function seedResolutions(preview: PlaylistImportPreviewResponse): Resolutions {
  const byPlaylist: Resolutions = new Map();
  preview.playlists.forEach((playlist, index) => {
    const byPosition = new Map<number, number | null>();
    for (const entry of playlist.entries) {
      byPosition.set(entry.position, entry.status === "matched" ? entry.item_id ?? null : null);
    }
    byPlaylist.set(index, byPosition);
  });
  return byPlaylist;
}

/**
 * The playlist-import flow: pick a source (uploaded m3u files or Plex
 * playlists), review each entry's library match, fix the ones that need it, and
 * commit. Two phases held in local state — the presence of a preview response
 * flips the page from source-selection to review.
 */
export function ImportPlaylistsPage() {
  const navigate = useNavigate();
  const [preview, setPreview] = useState<PlaylistImportPreviewResponse | null>(null);
  const [resolutions, setResolutions] = useState<Resolutions>(new Map());
  // Labels for picker-sourced picks (not in a preview entry's match/suggestions).
  const [picked, setPicked] = useState<Map<string, PickedTrack>>(new Map());
  const [pickerFor, setPickerFor] = useState<{ playlistIndex: number; position: number } | null>(
    null,
  );
  const [plexChecked, setPlexChecked] = useState<Set<string>>(new Set());
  // A file read that fails before any request still needs a visible reason.
  const [fileReadError, setFileReadError] = useState<string | null>(null);

  const previewMutation = useImportPreview();
  const commitMutation = useImportCommit();
  // Only the Plex source needs the server round-trip; enable it up front so both
  // sources are ready side by side. A 409 (unconfigured) / 502 (unreachable)
  // fails fast (retry:false) to the surfaced detail in the Plex card.
  const plexQuery = usePlexImportPlaylists(preview === null);

  function startReview(response: PlaylistImportPreviewResponse) {
    setResolutions(seedResolutions(response));
    setPicked(new Map());
    setPreview(response);
  }

  async function onFilesPicked(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    setFileReadError(null);
    let files: PlaylistImportFile[];
    try {
      files = await Promise.all(
        Array.from(fileList).map(async (file) => ({ name: file.name, content: await file.text() })),
      );
    } catch {
      setFileReadError("Couldn't read the selected files — try again.");
      return;
    }
    previewMutation.mutate({ files }, { onSuccess: startReview });
  }

  function previewFromPlex() {
    const names = [...plexChecked];
    if (names.length === 0) return;
    setFileReadError(null);
    previewMutation.mutate({ plex_playlists: names }, { onSuccess: startReview });
  }

  function setResolution(playlistIndex: number, position: number, itemId: number | null) {
    setResolutions((prev) => {
      const next = new Map(prev);
      const inner = new Map(next.get(playlistIndex) ?? []);
      inner.set(position, itemId);
      next.set(playlistIndex, inner);
      return next;
    });
  }

  function togglePlex(name: string, checked: boolean) {
    setPlexChecked((prev) => {
      const next = new Set(prev);
      if (checked) next.add(name);
      else next.delete(name);
      return next;
    });
  }

  function resolutionFor(playlistIndex: number, position: number): number | null {
    return resolutions.get(playlistIndex)?.get(position) ?? null;
  }

  /** The library track an entry currently resolves to (from its match, a
   * suggestion, or a picker pick) — for the "→ Title · Artist" confirmation. */
  function chosenTrack(playlistIndex: number, entry: ImportEntryPreview) {
    const chosen = resolutionFor(playlistIndex, entry.position);
    if (chosen === null) return null;
    if (entry.match && entry.match.item_id === chosen) return entry.match;
    const suggestion = entry.suggestions.find((track) => track.item_id === chosen);
    if (suggestion) return suggestion;
    const fromPicker = picked.get(pickedKey(playlistIndex, entry.position));
    if (fromPicker) {
      return { title: fromPicker.title, artist: fromPicker.artist };
    }
    return { title: `Track #${chosen}`, artist: "" };
  }

  function buildRequest(source: PlaylistImportPreviewResponse): PlaylistImportRequest {
    return {
      playlists: source.playlists.map((playlist, index) => ({
        name: playlist.name,
        description: "",
        entries: playlist.entries.map((entry) => {
          const itemId = resolutionFor(index, entry.position);
          if (itemId !== null) return { item_id: itemId };
          // Carry the source identity forward verbatim so the pending row keeps
          // enough to re-match (or export as-is) later.
          return {
            pending: {
              artist: entry.artist,
              title: entry.title,
              album: entry.album,
              duration_seconds: entry.duration_seconds,
              source: entry.source,
            },
          };
        }),
      })),
    };
  }

  function commit() {
    if (!preview) return;
    commitMutation.mutate(buildRequest(preview), {
      onSuccess: (response) => {
        // Surface a partial-import failure — the server creates what it can and
        // returns the rest in `failed`, so without this a dropped playlist would
        // vanish silently. (`?? []` guards a response that predates the field.)
        const failed = response.failed ?? [];
        if (failed.length > 0) {
          const names = failed.map((f) => f.name).join(", ");
          const total = response.created.length + failed.length;
          toast.error(`Imported ${response.created.length} of ${total} — couldn't create: ${names}`);
        } else if (response.created.length > 0) {
          const n = response.created.length;
          toast.success(`Imported ${n} ${n === 1 ? "playlist" : "playlists"}`);
        }
        navigate("/playlists");
      },
    });
  }

  // ── Review phase ──────────────────────────────────────────────────────────
  if (preview) {
    const count = preview.playlists.length;
    return (
      <section className="flex flex-col gap-6" aria-label="Review import">
        <PageHeader
          title="Review import"
          meta={`${count} ${count === 1 ? "playlist" : "playlists"} to create`}
          actions={
            <>
              <Button
                variant="outline"
                onClick={() => setPreview(null)}
                disabled={commitMutation.isPending}
              >
                Start over
              </Button>
              <Button onClick={commit} disabled={commitMutation.isPending}>
                {commitMutation.isPending
                  ? "Importing…"
                  : `Import ${count} ${count === 1 ? "playlist" : "playlists"}`}
              </Button>
            </>
          }
        />

        {commitMutation.isError && (
          <ErrorState
            variant="inline"
            message={commitMutation.error.message}
            onRetry={commit}
          />
        )}

        <ul className="flex flex-col gap-3">
          {preview.playlists.map((playlist, index) => (
            <li key={index}>
              <PlaylistReview
                playlist={playlist}
                resolutionFor={(position) => resolutionFor(index, position)}
                chosenTrack={(entry) => chosenTrack(index, entry)}
                onUseSuggestion={(position, itemId) =>
                  setResolution(index, position, itemId)
                }
                onSearch={(position) => setPickerFor({ playlistIndex: index, position })}
              />
            </li>
          ))}
        </ul>

        <TrackMatchPicker
          open={pickerFor !== null}
          onOpenChange={(open) => {
            if (!open) setPickerFor(null);
          }}
          onPick={(track) => {
            if (!pickerFor) return;
            setResolution(pickerFor.playlistIndex, pickerFor.position, track.item_id);
            setPicked((prev) => new Map(prev).set(pickedKey(pickerFor.playlistIndex, pickerFor.position), track));
            setPickerFor(null);
          }}
        />
      </section>
    );
  }

  // ── Source phase ──────────────────────────────────────────────────────────
  // A preview can fail from either source, and reading files can fail before
  // the request — surface both in one shared spot above the two source cards.
  const sourceError =
    fileReadError ?? (previewMutation.isError ? previewMutation.error.message : null);
  return (
    <section className="flex flex-col gap-6" aria-label="Import playlists">
      <PageHeader
        title="Import playlists"
        meta="Bring playlists in from m3u files or your Plex server."
        actions={
          <Button variant="outline" asChild>
            <Link to="/playlists">
              <Back className="size-4" aria-hidden="true" /> Playlists
            </Link>
          </Button>
        }
      />

      {sourceError && (
        <p className="text-destructive text-sm" role="alert">
          {sourceError}
        </p>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Upload className="size-4" aria-hidden="true" /> Upload playlist files
            </CardTitle>
            <CardDescription>
              Select one or more <code>.m3u</code> / <code>.m3u8</code> files. Each
              becomes a playlist; its lines are matched to your library.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <input
              type="file"
              multiple
              accept=".m3u,.m3u8"
              aria-label="Upload playlist files"
              disabled={previewMutation.isPending}
              onChange={(event) => {
                void onFilesPicked(event.target.files);
                event.target.value = ""; // allow re-picking the same file
              }}
              className="text-sm file:mr-3 file:rounded-md file:border file:border-input file:bg-transparent file:px-3 file:py-1.5 file:text-sm"
            />
            {previewMutation.isPending && (
              <p className="text-muted-foreground text-sm">Matching your library…</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Playlists className="size-4" aria-hidden="true" /> From Plex
            </CardTitle>
            <CardDescription>
              Pick playlists from your Plex server to pull in and match.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {plexQuery.isPending && <Skeleton className="h-16 w-full" />}
            {plexQuery.isError && (
              <p className="text-muted-foreground text-sm" role="alert">
                {plexQuery.error.message}
              </p>
            )}
            {plexQuery.data && plexQuery.data.length === 0 && (
              <p className="text-muted-foreground text-sm">No Plex playlists found.</p>
            )}
            {plexQuery.data && plexQuery.data.length > 0 && (
              <>
                <ul className="flex flex-col gap-2">
                  {plexQuery.data.map((playlist) => (
                    <li key={playlist.name} className="flex items-center gap-2">
                      <Checkbox
                        aria-label={playlist.name}
                        checked={plexChecked.has(playlist.name)}
                        onCheckedChange={(checked) =>
                          togglePlex(playlist.name, checked === true)
                        }
                      />
                      <span className="min-w-0 flex-1 truncate" title={playlist.name}>
                        {playlist.name}
                      </span>
                      <span className="text-muted-foreground shrink-0 text-sm tabular-nums">
                        {playlist.track_count}{" "}
                        {playlist.track_count === 1 ? "track" : "tracks"}
                      </span>
                    </li>
                  ))}
                </ul>
                <Button
                  className="self-start"
                  disabled={plexChecked.size === 0 || previewMutation.isPending}
                  onClick={previewFromPlex}
                >
                  Preview {plexChecked.size} from Plex
                </Button>
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </section>
  );
}

/** Live entry status — derived from the CURRENT resolution, not the preview's
 * frozen verdict. The header tallies already follow live state; a frozen badge
 * would contradict them the moment a chip is picked. */
type LiveStatus = "matched" | "ambiguous" | "unmatched";

function liveStatus(entry: ImportEntryPreview, resolved: number | null): LiveStatus {
  if (resolved !== null) return "matched";
  return entry.suggestions.length > 0 ? "ambiguous" : "unmatched";
}

const LIVE_STATUS_LABEL: Record<LiveStatus, string> = {
  matched: "Matched",
  ambiguous: "Ambiguous",
  unmatched: "Unmatched",
};

/** One previewed playlist: a summary row (name + match tallies) with an
 * expandable entry table behind it. Collapsed by default. */
function PlaylistReview({
  playlist,
  resolutionFor,
  chosenTrack,
  onUseSuggestion,
  onSearch,
}: {
  playlist: PlaylistImportPreview;
  resolutionFor: (position: number) => number | null;
  chosenTrack: (entry: ImportEntryPreview) => { title: string; artist: string } | null;
  onUseSuggestion: (position: number, itemId: number) => void;
  onSearch: (position: number) => void;
}) {
  // Tallies follow the LIVE resolution state, not the frozen preview counts: a
  // resolved entry (seeded match or a user pick) has an item_id, the rest are
  // still unmatched. So "2 ambiguous" doesn't linger after the user resolves them.
  const matched = playlist.entries.filter(
    (entry) => resolutionFor(entry.position) !== null,
  ).length;
  const unmatched = playlist.entries.length - matched;
  return (
    <Card>
      <details>
        <summary className="focus-ring flex cursor-pointer list-none items-center justify-between gap-3 rounded-xl px-4 py-3">
          <span className="min-w-0 truncate font-medium" title={playlist.name}>
            {playlist.name}
          </span>
          <span className="text-muted-foreground flex shrink-0 gap-2 text-sm tabular-nums">
            <span>{matched} matched</span>
            <span aria-hidden="true">·</span>
            <span>{unmatched} unmatched</span>
          </span>
        </summary>
        <ul className="divide-border flex flex-col divide-y border-t">
          {playlist.entries.map((entry) => (
            <EntryRow
              key={entry.position}
              entry={entry}
              resolved={resolutionFor(entry.position)}
              chosen={chosenTrack(entry)}
              onUseSuggestion={(itemId) => onUseSuggestion(entry.position, itemId)}
              onSearch={() => onSearch(entry.position)}
            />
          ))}
        </ul>
      </details>
    </Card>
  );
}

/** One entry: a single line — 1-based position, the track's own identity,
 * inline resolution (→ confirmation, or a Search escape when there are no
 * chips) and the live status badge — plus a second line ONLY when suggestion
 * chips exist. Suggestion-less unmatched rows (the common case on a big
 * import) stay one line tall. */
function EntryRow({
  entry,
  resolved,
  chosen,
  onUseSuggestion,
  onSearch,
}: {
  entry: ImportEntryPreview;
  resolved: number | null;
  chosen: { title: string; artist: string } | null;
  onUseSuggestion: (itemId: number) => void;
  onSearch: () => void;
}) {
  const status = liveStatus(entry, resolved);
  const hasSuggestions = entry.suggestions.length > 0;
  return (
    <li className="flex flex-col gap-2 px-4 py-3 last:rounded-b-xl">
      <div className="flex items-center gap-3">
        {/* 1-based for humans — the backend's positions are 0-based (m3u line
            order / Plex enumerate) and "track 0" reads like a bug. `position`
            itself stays the resolutions key; only the rendering shifts. */}
        <span className="text-muted-foreground w-8 shrink-0 text-right text-sm tabular-nums">
          {entry.position + 1}
        </span>
        {entry.title ? (
          // Lead with what the track IS — title · artist · duration, the thing
          // the user would search for — and demote the origin string to the
          // tooltip: on a Plex pull the source is just "plex:<playlist>" on
          // every row, which identifies nothing.
          <span className="min-w-0 flex-1 truncate text-sm" title={entry.source}>
            <span className="font-medium">{entry.title}</span>
            {entry.artist && (
              <span className="text-muted-foreground">
                <span aria-hidden="true"> &middot; </span>
                {entry.artist}
              </span>
            )}
            {entry.duration_seconds != null && (
              <span className="text-muted-foreground tabular-nums">
                <span aria-hidden="true"> &middot; </span>
                {formatDuration(entry.duration_seconds)}
              </span>
            )}
          </span>
        ) : (
          <span
            className="text-muted-foreground min-w-0 flex-1 truncate font-mono text-xs"
            title={entry.source}
          >
            {entry.source}
          </span>
        )}
        {chosen && (
          <span className="text-muted-foreground min-w-0 shrink truncate text-sm">
            <span aria-hidden="true">→ </span>
            <span className="text-foreground font-medium">{chosen.title}</span>
            {chosen.artist && ` · ${chosen.artist}`}
          </span>
        )}
        {!hasSuggestions && (
          <Button size="sm" variant="ghost" className="shrink-0" onClick={onSearch}>
            <Search className="size-4" aria-hidden="true" /> Search…
          </Button>
        )}
        <Badge
          variant={status === "matched" ? "secondary" : "outline"}
          className="shrink-0"
        >
          {LIVE_STATUS_LABEL[status]}
        </Badge>
      </div>

      {hasSuggestions && (
        // pl-11 = the position gutter (w-8) + the gap-3, so chips hang under
        // the title, not under the number.
        <div className="flex flex-wrap items-center gap-2 pl-11">
          <span aria-hidden="true" className="text-muted-foreground text-sm">
            ↳
          </span>
          {entry.suggestions.map((track) => {
            const active = resolved === track.item_id;
            return (
              <Button
                key={track.item_id}
                size="sm"
                variant={active ? "secondary" : "outline"}
                onClick={() => onUseSuggestion(track.item_id)}
              >
                {track.title}
                {track.duration_seconds !== null && (
                  <span className="text-muted-foreground ml-1 tabular-nums">
                    {formatDuration(track.duration_seconds)}
                  </span>
                )}
              </Button>
            );
          })}
          <Button size="sm" variant="ghost" onClick={onSearch}>
            <Search className="size-4" aria-hidden="true" /> Search…
          </Button>
        </div>
      )}
    </li>
  );
}
