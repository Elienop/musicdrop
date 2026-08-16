import { useState } from "react";

import {
  type Playlist,
  type PlaylistDetail,
  type PlaylistMergeResult,
  useMergePlaylist,
  usePlaylist,
  usePlaylists,
} from "@/api/usePlaylists";
import { Error as ErrorIcon, Spinner } from "@/components/icons";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/** How many source rows would come across, and how many are already here.
 *
 * Applies the SAME rule the server does - library item id only, so a pending
 * (grey) row, which has no id, always comes across and is never text-matched.
 * There is no dry-run endpoint, so this is a client-side ESTIMATE shown before
 * the human commits; the numbers announced AFTER the merge are the API's own
 * `added` / `skipped_duplicates`, never a recount. */
function preview(target: PlaylistDetail, sourceTracks: PlaylistDetail["tracks"]) {
  const here = new Set(
    target.tracks.map((t) => t.id).filter((id): id is number => id !== null),
  );
  const skipped = sourceTracks.filter((t) => t.id !== null && here.has(t.id)).length;
  return { added: sourceTracks.length - skipped, skipped };
}

function plural(n: number): string {
  return n === 1 ? "track" : "tracks";
}

/**
 * Fold another playlist into `target`. Pull, not push: you open the playlist
 * you want to end up complete and merge another one INTO it.
 *
 * Merge rewrites the whole list in one server round trip, so - unlike
 * reorder/remove on the detail page - it gets no optimistic path: the response
 * IS the new state. The dialog stays mounted while the POST is in flight so the
 * per-call `onSuccess` that announces and closes still belongs to a live
 * observer.
 */
export function MergePlaylistDialog({
  target,
  open,
  onOpenChange,
  onMerged,
}: {
  target: PlaylistDetail;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMerged: (result: PlaylistMergeResult, sourceName: string) => void;
}) {
  const list = usePlaylists();
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [deleteSource, setDeleteSource] = useState(false);
  const merge = useMergePlaylist(target.id);
  // Only the source's own DETAIL carries its rows; the summary has counts only.
  // `usePlaylist` is disabled on an empty id, so nothing fires until a pick.
  const sourceDetail = usePlaylist(sourceId ?? "");

  const candidates: Playlist[] = (list.data ?? []).filter((p) => p.id !== target.id);
  const source = candidates.find((p) => p.id === sourceId) ?? null;
  const counts = sourceDetail.data ? preview(target, sourceDetail.data.tracks) : null;

  function handleOpenChange(next: boolean) {
    // Never unmount mid-flight: the mutation's per-call onSuccess belongs to
    // this observer, and TanStack skips it once the observer is gone.
    if (merge.isPending) {
      return;
    }
    if (!next) {
      setSourceId(null);
      setDeleteSource(false);
    }
    onOpenChange(next);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Merge into &ldquo;{target.name}&rdquo;</DialogTitle>
          <DialogDescription>
            Pick a playlist to fold into this one. Its tracks are added at the end; nothing
            already here is reordered or removed.
          </DialogDescription>
        </DialogHeader>
        {/* min-w-0: DialogContent is a single-column grid whose min-width
            defaults to its content, so the truncating rows below would set the
            column minimum and push everything wider than the dialog box. */}
        <div className="flex min-w-0 flex-col gap-3">
          {/* pr-2: a gutter so the scrollbar doesn't sit on the row buttons. */}
          <div className="max-h-64 overflow-y-auto pr-2">
            {candidates.length === 0 ? (
              <p className="text-muted-foreground py-6 text-center text-sm">
                There is no other playlist to merge from.
              </p>
            ) : (
              <ul className="flex flex-col">
                {candidates.map((p) => (
                  <li key={p.id} className="border-b py-1 last:border-b-0">
                    {/* aria-pressed on a real <button>: there is no RadioGroup
                        in this project, and adding a role would replace the
                        element's own semantics rather than add to them. */}
                    <Button
                      variant={p.id === sourceId ? "secondary" : "ghost"}
                      className="w-full justify-start"
                      aria-pressed={p.id === sourceId}
                      onClick={() => setSourceId(p.id)}
                    >
                      <span className="min-w-0 truncate">{p.name}</span>
                      <span className="text-muted-foreground ml-auto shrink-0 text-sm tabular-nums">
                        {p.track_count} {plural(p.track_count)}
                      </span>
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {source && counts && (
            <p className="text-sm">
              {counts.added} {plural(counts.added)} will come across
              {counts.skipped > 0 ? `; ${counts.skipped} already here` : ""}.
            </p>
          )}

          {source && (
            <div className="flex items-center gap-2 text-sm">
              {/* aria-label carries the name: a wrapping <label> does not
                  associate with Radix's role="checkbox" button, so the visible
                  text alone would leave the control unnamed. */}
              <Checkbox
                checked={deleteSource}
                onCheckedChange={(next) => setDeleteSource(next === true)}
                aria-label={`Delete ${source.name} afterwards`}
              />
              <span aria-hidden="true">Delete {source.name} afterwards</span>
            </div>
          )}

          <p className="text-muted-foreground text-sm">
            Merging doesn&rsquo;t touch Plex: the merged playlist needs a sync to reach Plex.
          </p>

          {merge.isError && (
            <StatusBanner tone="destructive" icon={ErrorIcon}>
              Couldn&rsquo;t merge the playlists. Try again.
            </StatusBanner>
          )}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => handleOpenChange(false)}
            disabled={merge.isPending}
          >
            Cancel
          </Button>
          <Button
            disabled={source === null || merge.isPending}
            onClick={() => {
              if (source === null) {
                return;
              }
              const name = source.name;
              merge.mutate(
                { sourceId: source.id, deleteSource },
                {
                  onSuccess: (result) => {
                    onMerged(result, name);
                    setSourceId(null);
                    setDeleteSource(false);
                    onOpenChange(false);
                  },
                },
              );
            }}
          >
            {merge.isPending ? (
              <>
                <Spinner className="size-4 animate-spin" aria-hidden="true" />
                Merging&hellip;
              </>
            ) : (
              "Merge"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
