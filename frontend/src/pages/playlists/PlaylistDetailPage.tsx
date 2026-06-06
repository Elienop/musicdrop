import { AlertCircle, ArrowDown, ArrowUp, Check, Music, Pencil, Trash2, X } from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams } from "react-router";

import {
  type PlaylistDetail,
  type PlaylistTrack,
  useDeletePlaylist,
  usePlaylist,
  useRemoveTrack,
  useRenamePlaylist,
  useReorderTracks,
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

  const [editingName, setEditingName] = useState(false);
  const [draftName, setDraftName] = useState(playlist.name);

  const order = playlist.tracks.map((t) => t.id);

  function saveName() {
    const next = draftName.trim();
    if (next.length === 0 || next === playlist.name) {
      setEditingName(false);
      return;
    }
    rename.mutate({ name: next }, { onSuccess: () => setEditingName(false) });
  }

  /** Move the track at `index` one slot in `dir` and PUT the new full order. */
  function move(index: number, dir: -1 | 1) {
    const target = index + dir;
    if (target < 0 || target >= order.length) {
      return;
    }
    const next = [...order];
    [next[index], next[target]] = [next[target], next[index]];
    reorder.mutate(next);
  }

  return (
    <section className="flex flex-col gap-6" aria-label="Playlist">
      <BackLink to="/playlists" label="Playlists" />

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
            {playlist.track_count} {playlist.track_count === 1 ? "track" : "tracks"}
          </p>
        </div>

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
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                variant="destructive"
                onClick={() =>
                  remove.mutate(playlist.id, {
                    onSuccess: () => navigate("/playlists"),
                  })
                }
              >
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </header>

      {rename.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t rename the playlist. Try again.
        </p>
      )}

      {playlist.tracks.length === 0 ? (
        <EmptyTracks />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12 pr-4 text-right">#</TableHead>
              <TableHead>Title</TableHead>
              <TableHead className="w-20 text-right">Length</TableHead>
              <TableHead className="w-28 text-right">Reorder</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {playlist.tracks.map((track, index) => (
              <PlaylistTrackRow
                key={track.id}
                track={track}
                position={index + 1}
                isFirst={index === 0}
                isLast={index === playlist.tracks.length - 1}
                reordering={reorder.isPending}
                onMoveUp={() => move(index, -1)}
                onMoveDown={() => move(index, 1)}
                onRemove={() => removeTrack.mutate(track.id)}
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
  reordering,
  onMoveUp,
  onMoveDown,
  onRemove,
}: {
  track: PlaylistTrack;
  position: number;
  isFirst: boolean;
  isLast: boolean;
  reordering: boolean;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
}) {
  // A track whose beets item no longer resolves: keep its slot (so it can be
  // removed) but grey it and label the gap.
  const title = track.available ? track.title : track.title || "(removed track)";
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
                Unavailable
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
            size="icon-sm"
            variant="ghost"
            onClick={onMoveUp}
            disabled={isFirst || reordering}
            aria-label={`Move ${title} up`}
          >
            <ArrowUp className="size-4" aria-hidden="true" />
          </Button>
          <Button
            size="icon-sm"
            variant="ghost"
            onClick={onMoveDown}
            disabled={isLast || reordering}
            aria-label={`Move ${title} down`}
          >
            <ArrowDown className="size-4" aria-hidden="true" />
          </Button>
          <Button
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

function EmptyTracks() {
  return (
    <div className="rounded-xl border border-dashed p-8 text-center" role="status">
      <Music className="text-muted-foreground mx-auto mb-2 size-8" aria-hidden="true" />
      <p className="font-medium">No tracks yet</p>
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
