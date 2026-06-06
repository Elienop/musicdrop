import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  Check,
  Loader2,
  Music,
  Pencil,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";

import {
  type PlaylistDetail,
  type PlaylistTrack,
  useDeletePlaylist,
  usePlaylist,
  useRemoveTrack,
  useRenamePlaylist,
  useReorderTracks,
  useSyncPlaylist,
} from "@/api/usePlaylists";
import { BackLink } from "@/components/albums/album-grid";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/** Format a duration in seconds as `m:ss`, en-dash for missing. Mirrors the
 * album/search tracklists. */
function formatDuration(seconds: number | null): string {
  if (seconds === null) {
    return "–";
  }
  const total = Math.floor(seconds);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

/** What the row shows (and what we announce): a vanished beets item keeps its
 * slot but reads as "(removed track)" when it has no title. */
function displayTitle(track: PlaylistTrack): string {
  return track.available ? track.title : track.title || "(removed track)";
}

/** The one-line Plex sync state derived from the playlist's recorded `admin`
 * target. "Out of date" wins when the playlist changed after its last push. */
function plexStatusLabel(playlist: PlaylistDetail): string {
  const admin = playlist.plex?.admin;
  if (!admin) {
    return "Not synced to Plex";
  }
  if (admin.synced_at != null && playlist.updated_at > admin.synced_at) {
    return "Out of date — re-sync";
  }
  if (admin.status === "ok") {
    return "Synced";
  }
  if (admin.status === "partial") {
    return `${admin.missing} not in Plex`;
  }
  if (admin.status === "empty") {
    return "No matching tracks";
  }
  return "Not synced to Plex";
}

export function PlaylistDetailPage() {
  const { playlistId } = useParams<{ playlistId: string }>();
  const id = playlistId ?? "";
  const { data, isPending, isError, refetch } = usePlaylist(id);

  if (isPending) {
    return <DetailSkeleton />;
  }
  if (isError) {
    return <ErrorState onRetry={() => void refetch()} />;
  }
  return <PlaylistDetailView playlist={data} />;
}

function PlaylistDetailView({ playlist }: { playlist: PlaylistDetail }) {
  const navigate = useNavigate();
  const rename = useRenamePlaylist(playlist.id);
  const remove = useDeletePlaylist();
  const reorder = useReorderTracks(playlist.id);
  const removeTrack = useRemoveTrack(playlist.id);
  const sync = useSyncPlaylist(playlist.id);

  const [editingName, setEditingName] = useState(false);
  const [draftName, setDraftName] = useState(playlist.name);

  // Local copy of the tracklist so reorder/remove update the UI immediately
  // (optimistic) and keyboard focus can be restored deterministically once the
  // new row order has rendered — instead of waiting on the refetch round-trip.
  // Re-seeded whenever the server hands back a fresh tracklist.
  const [tracks, setTracks] = useState(playlist.tracks);
  useEffect(() => {
    setTracks(playlist.tracks);
  }, [playlist.tracks]);

  // Single polite live region for reorder/remove announcements.
  const [statusMsg, setStatusMsg] = useState("");

  // Focus restoration: a move reshuffles rows and a remove unmounts one, both of
  // which drop keyboard focus to <body>. We record which control to refocus and
  // apply it in a layout effect after the new order paints. Buttons are keyed by
  // `${trackId}:up|down|remove` so the lookup survives reordering.
  const buttonRefs = useRef(new Map<string, HTMLButtonElement | null>());
  const emptyRef = useRef<HTMLParagraphElement | null>(null);
  const pendingFocus = useRef<{ keys: string[]; empty?: boolean } | null>(null);

  function setButtonRef(key: string, el: HTMLButtonElement | null) {
    if (el) {
      buttonRefs.current.set(key, el);
    } else {
      buttonRefs.current.delete(key);
    }
  }

  useLayoutEffect(() => {
    const req = pendingFocus.current;
    if (!req) {
      return;
    }
    pendingFocus.current = null;
    for (const key of req.keys) {
      const el = buttonRefs.current.get(key);
      if (el && !el.disabled) {
        el.focus();
        return;
      }
    }
    if (req.empty) {
      emptyRef.current?.focus();
    }
  }, [tracks]);

  function saveName() {
    const next = draftName.trim();
    if (next.length === 0 || next === playlist.name) {
      setEditingName(false);
      return;
    }
    rename.mutate({ name: next }, { onSuccess: () => setEditingName(false) });
  }

  /** Move the track at `index` one slot in `dir`: reorder locally for instant
   * feedback, PUT the new full order, and refocus the moved row's move button
   * (falling back to its sibling button when the move lands it at an end). */
  function move(index: number, dir: -1 | 1) {
    const target = index + dir;
    if (target < 0 || target >= tracks.length) {
      return;
    }
    const prev = tracks;
    const next = [...tracks];
    [next[index], next[target]] = [next[target], next[index]];
    setTracks(next);

    const moved = next[target];
    setStatusMsg(`Moved ${displayTitle(moved)} to position ${target + 1}`);
    const dirKey = dir === -1 ? "up" : "down";
    const altKey = dir === -1 ? "down" : "up";
    pendingFocus.current = { keys: [`${moved.id}:${dirKey}`, `${moved.id}:${altKey}`] };

    reorder.mutate(
      next.map((t) => t.id),
      { onError: () => setTracks(prev) },
    );
  }

  /** Remove the track at `index`. On success announce it and move focus to a
   * surviving sibling (the row that shifts up into its slot, else the previous
   * row, else the empty-state heading) — the refetch unmounts the row and the
   * layout effect applies the queued focus once the new order paints. */
  function handleRemove(index: number) {
    const removed = tracks[index];
    const afterRemoval = tracks.filter((_, i) => i !== index);
    removeTrack.mutate(removed.id, {
      onSuccess: () => {
        setStatusMsg(`Removed ${displayTitle(removed)}`);
        if (afterRemoval.length === 0) {
          pendingFocus.current = { keys: [], empty: true };
        } else {
          const survivor = afterRemoval[Math.min(index, afterRemoval.length - 1)];
          pendingFocus.current = { keys: [`${survivor.id}:remove`] };
        }
      },
    });
  }

  return (
    <section className="flex flex-col gap-6" aria-label="Playlist">
      <BackLink to="/playlists" label="Playlists" />

      <p className="sr-only" role="status" aria-live="polite">
        {statusMsg}
      </p>

      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-2">
          {editingName ? (
            <div className="flex items-center gap-2">
              <Input
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                aria-label="Playlist name"
                className="h-9 w-64 max-w-full"
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") saveName();
                  if (e.key === "Escape") setEditingName(false);
                }}
              />
              <Button
                size="icon-sm"
                onClick={saveName}
                disabled={rename.isPending}
                aria-label="Save name"
              >
                <Check className="size-4" aria-hidden="true" />
              </Button>
              <Button
                size="icon-sm"
                variant="ghost"
                onClick={() => {
                  setDraftName(playlist.name);
                  setEditingName(false);
                }}
                aria-label="Cancel rename"
              >
                <X className="size-4" aria-hidden="true" />
              </Button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <h2 className="truncate text-2xl font-semibold tracking-tight" title={playlist.name}>
                {playlist.name}
              </h2>
              <Button
                size="icon-sm"
                variant="ghost"
                onClick={() => {
                  setDraftName(playlist.name);
                  setEditingName(true);
                }}
                aria-label="Edit name"
              >
                <Pencil className="size-4" aria-hidden="true" />
              </Button>
            </div>
          )}
          <p className="text-muted-foreground text-sm">
            {tracks.length} {tracks.length === 1 ? "track" : "tracks"}
          </p>
          <p className="text-muted-foreground text-sm">{plexStatusLabel(playlist)}</p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => sync.mutate(undefined, { onSuccess: () => setStatusMsg("Plex sync complete") })}
            disabled={sync.isPending}
          >
            {sync.isPending ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                Syncing&hellip;
              </>
            ) : (
              <>
                <RefreshCw className="size-4" aria-hidden="true" /> Sync to Plex
              </>
            )}
          </Button>

          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="outline" size="sm">
                <Trash2 className="size-4" aria-hidden="true" /> Delete playlist
              </Button>
            </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete &ldquo;{playlist.name}&rdquo;?</AlertDialogTitle>
              <AlertDialogDescription>
                This removes the playlist and its exported file. Your tracks stay in the
                library.
              </AlertDialogDescription>
            </AlertDialogHeader>
            {remove.isError && (
              <p className="text-destructive text-sm" role="alert">
                Couldn&rsquo;t delete the playlist. Try again.
              </p>
            )}
            <AlertDialogFooter>
              <AlertDialogCancel disabled={remove.isPending}>Cancel</AlertDialogCancel>
              <AlertDialogAction
                variant="destructive"
                disabled={remove.isPending}
                onClick={(e) => {
                  // Keep the dialog mounted while the DELETE is in flight: it
                  // would otherwise auto-close on click, hiding the pending state
                  // and any error. We navigate away ourselves on success.
                  e.preventDefault();
                  remove.mutate(playlist.id, {
                    onSuccess: () => navigate("/playlists"),
                  });
                }}
              >
                {remove.isPending ? (
                  <>
                    <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                    Deleting&hellip;
                  </>
                ) : (
                  "Delete"
                )}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
          </AlertDialog>
        </div>
      </header>

      {rename.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t rename the playlist. Try again.
        </p>
      )}
      {reorder.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t save the new order. Try again.
        </p>
      )}
      {removeTrack.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t remove the track. Try again.
        </p>
      )}
      {sync.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t sync to Plex. Try again.
        </p>
      )}

      {tracks.length === 0 ? (
        <EmptyTracks headingRef={emptyRef} />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12 pr-4 text-right">#</TableHead>
              <TableHead>Title</TableHead>
              <TableHead className="w-20 text-right">Length</TableHead>
              <TableHead className="w-28 text-right">
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tracks.map((track, index) => (
              <PlaylistTrackRow
                key={track.id}
                track={track}
                position={index + 1}
                isFirst={index === 0}
                isLast={index === tracks.length - 1}
                onMoveUp={() => move(index, -1)}
                onMoveDown={() => move(index, 1)}
                onRemove={() => handleRemove(index)}
                registerRef={setButtonRef}
              />
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}

function PlaylistTrackRow({
  track,
  position,
  isFirst,
  isLast,
  onMoveUp,
  onMoveDown,
  onRemove,
  registerRef,
}: {
  track: PlaylistTrack;
  position: number;
  isFirst: boolean;
  isLast: boolean;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
  registerRef: (key: string, el: HTMLButtonElement | null) => void;
}) {
  // A track whose beets item no longer resolves: keep its slot (so it can be
  // removed) but grey it and label the gap.
  const title = displayTitle(track);
  return (
    <TableRow className={track.available ? undefined : "bg-muted/40"}>
      <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
        {position}
      </TableCell>
      <TableCell>
        <div className="flex min-w-0 flex-col">
          <div className="flex items-center gap-2">
            <span
              className={`truncate font-medium ${track.available ? "" : "text-muted-foreground italic"}`}
            >
              {title}
            </span>
            {!track.available && (
              <Badge variant="outline" className="shrink-0 text-xs font-normal">
                unavailable
              </Badge>
            )}
          </div>
          {track.available && (
            <span className="text-muted-foreground truncate text-sm">
              {track.artist}
              {track.album && (
                <>
                  <span aria-hidden="true"> &middot; </span>
                  {track.album}
                </>
              )}
            </span>
          )}
        </div>
      </TableCell>
      <TableCell className="text-muted-foreground text-right tabular-nums">
        {formatDuration(track.duration_seconds)}
      </TableCell>
      <TableCell>
        <div className="flex items-center justify-end gap-1">
          <Button
            ref={(el) => registerRef(`${track.id}:up`, el)}
            size="icon-sm"
            variant="ghost"
            onClick={onMoveUp}
            disabled={isFirst}
            aria-label={`Move ${title} up`}
          >
            <ArrowUp className="size-4" aria-hidden="true" />
          </Button>
          <Button
            ref={(el) => registerRef(`${track.id}:down`, el)}
            size="icon-sm"
            variant="ghost"
            onClick={onMoveDown}
            disabled={isLast}
            aria-label={`Move ${title} down`}
          >
            <ArrowDown className="size-4" aria-hidden="true" />
          </Button>
          <Button
            ref={(el) => registerRef(`${track.id}:remove`, el)}
            size="icon-sm"
            variant="ghost"
            onClick={onRemove}
            aria-label={`Remove ${title}`}
          >
            <X className="size-4" aria-hidden="true" />
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
}

function EmptyTracks({
  headingRef,
}: {
  headingRef?: React.Ref<HTMLParagraphElement>;
}) {
  return (
    <div className="rounded-xl border border-dashed p-8 text-center" role="status">
      <Music className="text-muted-foreground mx-auto mb-2 size-8" aria-hidden="true" />
      <p ref={headingRef} tabIndex={-1} className="font-medium outline-none">
        No tracks yet
      </p>
      <p className="text-muted-foreground text-sm">
        Add some from an album or search.
      </p>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-hidden="true">
      <Skeleton className="h-8 w-32" />
      <Skeleton className="h-8 w-64" />
      <div className="flex flex-col gap-2">
        {Array.from({ length: 6 }, (_, i) => (
          <div key={i} className="flex items-center gap-4 py-1">
            <Skeleton className="h-5 w-6 shrink-0" />
            <Skeleton className="h-5 flex-1" />
            <Skeleton className="ml-auto h-5 w-12 shrink-0" />
          </div>
        ))}
      </div>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col gap-6">
      <BackLink to="/playlists" label="Playlists" />
      <div className="border-destructive/40 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border py-16 text-center">
        <AlertCircle className="text-destructive size-10" aria-hidden="true" />
        <div className="flex flex-col gap-1">
          <p className="font-medium">Couldn&rsquo;t load this playlist</p>
          <p className="text-muted-foreground text-sm">
            The library didn&rsquo;t respond. Check the backend and try again.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      </div>
    </div>
  );
}
