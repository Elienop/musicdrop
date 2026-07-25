import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";

import {
  type ImportEntryPreview,
  type PlaylistImportFile,
  type PlaylistImportPreview,
  type PlaylistImportPreviewResponse,
  type PlaylistImportRequest,
  type PlexPlaylistInfo,
  useImportCommit,
  useImportPreview,
  usePlexImportPlaylists,
} from "@/api/usePlaylistImport";
import { Back, Close, Edit, Playlists, Search, Success, Upload } from "@/components/icons";
import { type PickedTrack, TrackMatchPicker } from "@/components/playlists/TrackMatchPicker";
import { ErrorState } from "@/components/system/ErrorState";
import { PageHeader } from "@/components/system/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
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

/** One accessible checkbox name per listed Plex playlist, by index.
 *
 * Plex allows duplicate titles, so a repeated title is qualified with its track
 * count — and when THAT still ties (same title AND same count, the commonest
 * duplicate shape) a 1-based ordinal is appended. Every row therefore reads
 * distinctly, which is the whole point: two identically-named checkboxes cannot
 * be told apart by a screen reader or by name-based test queries.
 */
function plexLabels(playlists: PlexPlaylistInfo[]): string[] {
  const byTitle = new Map<string, number>();
  const byTitleAndCount = new Map<string, number>();
  const key = (p: PlexPlaylistInfo) => `${p.name}\u0000${p.track_count}`;
  for (const p of playlists) {
    byTitle.set(p.name, (byTitle.get(p.name) ?? 0) + 1);
    byTitleAndCount.set(key(p), (byTitleAndCount.get(key(p)) ?? 0) + 1);
  }
  const ordinals = new Map<string, number>();
  return playlists.map((p) => {
    if ((byTitle.get(p.name) ?? 0) < 2) return p.name;
    const tracks = `${p.track_count} ${p.track_count === 1 ? "track" : "tracks"}`;
    const label = `${p.name} · ${tracks}`;
    if ((byTitleAndCount.get(key(p)) ?? 0) < 2) return label;
    const nth = (ordinals.get(key(p)) ?? 0) + 1;
    ordinals.set(key(p), nth);
    return `${label} · #${nth}`;
  });
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
  // Per-playlist name edits, keyed by the preview INDEX (names aren't unique).
  // Seeded from the preview at review start; an empty/whitespace edit falls back
  // to the original name at commit.
  const [names, setNames] = useState<Map<number, string>>(new Map());
  // The Plex rating keys THIS preview was pulled with, aligned to the preview
  // list by index (the backend returns one preview per requested key, in order),
  // or null for a file upload. Only a Plex-sourced import stamps each playlist's
  // `plex_rating_key` / `plex_source`; file uploads never do.
  const [plexKeys, setPlexKeys] = useState<string[] | null>(null);
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

  function startReview(response: PlaylistImportPreviewResponse, sourceKeys: string[] | null) {
    setResolutions(seedResolutions(response));
    setPicked(new Map());
    setNames(new Map(response.playlists.map((playlist, index) => [index, playlist.name])));
    setPlexKeys(sourceKeys);
    setPreview(response);
  }

  /** Update the edited name for the playlist at `index`. */
  function setPlaylistName(index: number, value: string) {
    setNames((prev) => new Map(prev).set(index, value));
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
      setFileReadError("Couldn't read the selected files. Try again.");
      return;
    }
    previewMutation.mutate({ files }, { onSuccess: (response) => startReview(response, null) });
  }

  function previewFromPlex() {
    const selected = [...plexChecked];
    if (selected.length === 0) return;
    setFileReadError(null);
    previewMutation.mutate(
      { plex_rating_keys: selected },
      { onSuccess: (response) => startReview(response, selected) },
    );
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

  /** Select/deselect one listed Plex playlist BY ITS RATING KEY — titles aren't
   * unique, so a name-keyed set would make one of a same-titled pair
   * unselectable (and both resolve to the same playlist server-side). */
  function togglePlex(ratingKey: string, checked: boolean) {
    setPlexChecked((prev) => {
      const next = new Set(prev);
      if (checked) next.add(ratingKey);
      else next.delete(ratingKey);
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
        // A blank/whitespace edit falls back to the original preview name.
        name: (names.get(index) ?? "").trim() || playlist.name,
        description: "",
        // Only a Plex-sourced import carries these two, and only together:
        //  · plex_rating_key is the IDENTITY the backend resolves the poster by
        //    (the same key this preview was pulled with, aligned by index).
        //  · plex_source is the ORIGINAL bare Plex title (never the edited name,
        //    never a decorated "plex:<name>") — the display value, and the
        //    backend's title fallback for keyless legacy bodies.
        ...(plexKeys
          ? { plex_source: playlist.name, plex_rating_key: plexKeys[index] }
          : {}),
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
          toast.error(`Imported ${response.created.length} of ${total}; couldn't create: ${names}`);
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
                name={names.get(index) ?? playlist.name}
                onRename={(value) => setPlaylistName(index, value)}
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
  // Plex allows duplicate titles — qualify those rows' labels so both are
  // addressable (the identity itself is the rating_key, not the label).
  const plexAriaLabels = plexLabels(plexQuery.data ?? []);
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
                  {plexQuery.data.map((playlist, index) => (
                    // Keyed by rating_key: two Plex playlists can share a title,
                    // and a name key would collapse them into one row.
                    <li key={playlist.rating_key} className="flex items-center gap-2">
                      <Checkbox
                        aria-label={plexAriaLabels[index]}
                        checked={plexChecked.has(playlist.rating_key)}
                        onCheckedChange={(checked) =>
                          togglePlex(playlist.rating_key, checked === true)
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
 * expandable entry list behind it. Collapsed by default. The list opens on
 * the needs-attention view — the rows still awaiting a decision — with the
 * resolved ones a toggle away, so a big import isn't buried under rows that
 * are already done. */
function PlaylistReview({
  playlist,
  name,
  onRename,
  resolutionFor,
  chosenTrack,
  onUseSuggestion,
  onSearch,
}: {
  playlist: PlaylistImportPreview;
  name: string;
  onRename: (value: string) => void;
  resolutionFor: (position: number) => number | null;
  chosenTrack: (entry: ImportEntryPreview) => { title: string; artist: string } | null;
  onUseSuggestion: (position: number, itemId: number) => void;
  onSearch: (position: number) => void;
}) {
  const [view, setView] = useState<"attention" | "all">("attention");
  // Inline name editor (the detail-page rename idiom: pencil → input → save).
  // The committed value lives in page state (`names`); the draft is local while
  // editing, and a blank save is rejected so the card keeps its original title.
  const [editingName, setEditingName] = useState(false);
  const [draftName, setDraftName] = useState(name);
  function saveName() {
    const next = draftName.trim();
    if (next.length === 0 || next === name) {
      setEditingName(false);
      return;
    }
    onRename(next);
    setEditingName(false);
  }
  // Tallies follow the LIVE resolution state, not the frozen preview counts: a
  // resolved entry (seeded match or a user pick) has an item_id, the rest are
  // still unmatched. So "2 ambiguous" doesn't linger after the user resolves them.
  const matched = playlist.entries.filter(
    (entry) => resolutionFor(entry.position) !== null,
  ).length;
  const unmatched = playlist.entries.length - matched;
  // Resolving a row in the attention view drops it immediately — the list
  // shrinks as you work (the Review-page bank posture). It stays reachable,
  // and re-pickable, under All.
  const visible =
    view === "all"
      ? playlist.entries
      : playlist.entries.filter((entry) => resolutionFor(entry.position) === null);
  return (
    <Card>
      <details>
        <summary className="focus-ring flex cursor-pointer list-none items-center justify-between gap-3 rounded-xl px-4 py-3">
          {editingName ? (
            // The editor sits inside <summary>, so its clicks preventDefault —
            // otherwise focusing the input or hitting save/cancel would toggle
            // the <details> collapse.
            <span
              className="flex min-w-0 flex-1 items-center gap-2"
              onClick={(e) => e.preventDefault()}
            >
              <Input
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                aria-label="Playlist name"
                className="h-8 w-64 max-w-full"
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    saveName();
                  }
                  if (e.key === "Escape") setEditingName(false);
                }}
              />
              <Button size="icon-sm" onClick={saveName} aria-label="Save name">
                <Success className="size-4" aria-hidden="true" />
              </Button>
              <Button
                size="icon-sm"
                variant="ghost"
                onClick={() => setEditingName(false)}
                aria-label="Cancel rename"
              >
                <Close className="size-4" aria-hidden="true" />
              </Button>
            </span>
          ) : (
            <span className="flex min-w-0 flex-1 items-center gap-2">
              <span className="min-w-0 truncate font-medium" title={name}>
                {name}
              </span>
              {/* The pencil preventDefaults too — a rename shouldn't toggle the
                  card open/closed. The name text itself still toggles. */}
              <span className="shrink-0" onClick={(e) => e.preventDefault()}>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  onClick={() => {
                    setDraftName(name);
                    setEditingName(true);
                  }}
                  aria-label={`Rename ${name}`}
                >
                  <Edit className="size-4" aria-hidden="true" />
                </Button>
              </span>
            </span>
          )}
          <span className="flex shrink-0 items-center gap-3">
            <span className="text-muted-foreground text-sm tabular-nums">
              {matched} matched
            </span>
            {/* The filter lives INSIDE <summary> now, wrapped so its clicks
                preventDefault — otherwise activating an option would toggle the
                <details> collapse instead of switching the view. */}
            <span onClick={(e) => e.preventDefault()}>
              <SegmentedControl
                aria-label="Filter entries"
                value={view}
                onChange={(v) => setView(v === "all" ? "all" : "attention")}
                options={[
                  { value: "attention", label: `Needs attention (${unmatched})` },
                  { value: "all", label: `All (${playlist.entries.length})` },
                ]}
              />
            </span>
          </span>
        </summary>
        {visible.length === 0 ? (
          <p className="text-muted-foreground border-t px-4 py-3 text-sm">
            Everything’s matched. Switch to All to see the entries.
          </p>
        ) : (
          <ul className="divide-border flex flex-col divide-y border-t">
            {visible.map((entry) => (
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
        )}
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
            {chosen.artist && (
              <span>
                <span aria-hidden="true"> · </span>
                {chosen.artist}
              </span>
            )}
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
