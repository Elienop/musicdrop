import {
  AlertCircle,
  AlertTriangle,
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
import { Link, useNavigate, useParams } from "react-router";

import {
  type PlaylistDetail,
  type PlaylistTrack,
  useDeletePlaylist,
  usePlaylist,
  useRemoveTrack,
  useRenamePlaylist,
  useReorderTracks,
  useSetTargets,
  useSyncPlaylist,
} from "@/api/usePlaylists";
import { usePlexUsers } from "@/api/usePlex";
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
import { Checkbox } from "@/components/ui/checkbox";
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
import { formatDuration } from "@/lib/format";

/** What the row shows (and what we announce): a vanished beets item keeps its
 * slot but reads as "(removed track)" when it has no title. */
function displayTitle(track: PlaylistTrack): string {
  return track.available ? track.title : track.title || "(removed track)";
}

type PlexTargetState = PlaylistDetail["plex"][string];

/** Visual tone for a sync status, mapped to a leading icon + a semantic text
 * color so the state reads at a glance (the text label stays the non-color
 * carrier for screen readers / color-blind users). */
type StatusTone = "success" | "warning" | "destructive" | "muted";

interface SyncStatus {
  label: string;
  tone: StatusTone;
}

/** True when ISO instant `a` is strictly later than `b`. Compares parsed
 * instants (not raw strings) so it's robust to timezone-offset / sub-second
 * precision drift between the two timestamps. */
function isAfter(a: string, b: string): boolean {
  return new Date(a).getTime() > new Date(b).getTime();
}

/** The one-line Plex sync status for one target (admin or a fan-out user).
 * "Out of date" wins when the playlist changed after this target's last push.
 * An absent/unknown state falls back to `notSyncedLabel` (e.g. a freshly-checked
 * target that hasn't synced yet). */
function syncStatus(
  state: PlexTargetState | undefined,
  playlist: PlaylistDetail,
  notSyncedLabel: string,
): SyncStatus {
  if (!state) {
    return { label: notSyncedLabel, tone: "muted" };
  }
  if (state.synced_at != null && isAfter(playlist.updated_at, state.synced_at)) {
    return { label: "Out of date — re-sync", tone: "warning" };
  }
  switch (state.status) {
    case "ok":
      return { label: "Synced", tone: "success" };
    case "partial":
      return { label: `${state.missing} not in Plex`, tone: "warning" };
    case "empty":
      return { label: "No matching tracks", tone: "muted" };
    case "failed":
      return { label: state.error ?? "Failed", tone: "destructive" };
    default:
      return { label: notSyncedLabel, tone: "muted" };
  }
}

/** The owner's own copy: the `admin` target, with a Plex-specific "not synced"
 * label. */
function adminSyncStatus(playlist: PlaylistDetail): SyncStatus {
  return syncStatus(playlist.plex?.admin, playlist, "Not synced to Plex");
}

/** Render a sync status with a leading lucide icon + semantic color. The text
 * label is always present (the color/icon are emphasis, not the only signal). */
function StatusLine({ status }: { status: SyncStatus }) {
  const { label, tone } = status;
  const Icon =
    tone === "success"
      ? Check
      : tone === "warning"
        ? AlertTriangle
        : tone === "destructive"
          ? AlertCircle
          : null;
  const colorClass =
    tone === "success"
      ? "text-success"
      : tone === "warning"
        ? "text-warning"
        : tone === "destructive"
          ? "text-destructive"
          : "text-muted-foreground";
  return (
    <span className={`inline-flex items-center gap-1 ${colorClass}`}>
      {Icon ? <Icon className="size-3.5 shrink-0" aria-hidden="true" /> : null}
      {label}
    </span>
  );
}

/** A first-time sync fails with a 409 when Plex isn't connected yet (no base
 * URL / token). `useSyncPlaylist` tags the thrown error with the HTTP status so
 * we can point the user at Settings instead of showing the generic retry copy. */
function isPlexNotConfigured(err: unknown): boolean {
  return err instanceof Error && (err as { status?: number }).status === 409;
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
  const plexUsers = usePlexUsers();
  const setTargets = useSetTargets(playlist.id);

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

  // Optimistic copy of the fan-out target set so a toggled checkbox reflects
  // intent immediately (no wait on the PATCH round-trip) and stays ENABLED while
  // the save is in flight — disabling it on a shared `isPending` would strand
  // keyboard focus and block every other checkbox. Re-seeded from the server on
  // settle (the refetch reverts the optimistic state if the save failed).
  const [targetIds, setTargetIds] = useState(() => new Set(playlist.target_plex_users));
  useEffect(() => {
    setTargetIds(new Set(playlist.target_plex_users));
  }, [playlist.target_plex_users]);

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

  /** Add/remove a Plex Home user from this playlist's fan-out targets. Updates
   * the optimistic set immediately, then PATCHes the new full list. Announces the
   * specific action (so consecutive saves re-announce) via the live region. A
   * newly-added account reads "Not synced yet" until the next sync pushes its
   * copy. */
  function toggleTarget(user: { id: string; name: string }) {
    const checked = targetIds.has(user.id);
    const next = new Set(targetIds);
    if (checked) {
      next.delete(user.id);
    } else {
      next.add(user.id);
    }
    setTargetIds(next);
    setStatusMsg(
      `${checked ? "Removed" : "Added"} ${user.name} ${checked ? "from" : "to"} Plex sync`,
    );
    setTargets.mutate([...next]);
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
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              sync.mutate(undefined, {
                // Announce the real outcome (synced / N not in Plex / no
                // matching tracks) instead of a blanket "complete" — derived
                // from the same label the visible status line shows.
                onSuccess: (updated) =>
                  setStatusMsg(`Plex sync — ${adminSyncStatus(updated).label}`),
              })
            }
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
                {Object.keys(playlist.plex ?? {}).length > 0 ? (
                  <>
                    {" "}
                    This also removes it from Plex ({Object.keys(playlist.plex).length} account
                    {Object.keys(playlist.plex).length === 1 ? "" : "s"}).
                  </>
                ) : null}
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
      {sync.isError &&
        (isPlexNotConfigured(sync.error) ? (
          <p className="text-destructive text-sm" role="alert">
            Connect Plex in{" "}
            <Link to="/settings" className="underline">
              Settings
            </Link>{" "}
            first.
          </p>
        ) : (
          <p className="text-destructive text-sm" role="alert">
            Couldn&rsquo;t sync to Plex. Try again.
          </p>
        ))}
      {setTargets.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t save the Plex targets. Try again.
        </p>
      )}

      <section
        className="flex flex-col gap-2 rounded-xl border p-4"
        aria-label="Plex sync"
      >
        <h3 className="text-sm font-semibold">Plex sync</h3>
        <ul className="flex flex-col gap-2">
          {/* The owner always gets their own copy — shown first, no checkbox. */}
          <li className="flex items-center justify-between gap-3 text-sm">
            <span className="font-medium">You (admin)</span>
            <StatusLine status={adminSyncStatus(playlist)} />
          </li>
          {plexUsers.isError ? (
            isPlexNotConfigured(plexUsers.error) ? (
              <li className="text-muted-foreground text-sm">
                Connect Plex in{" "}
                <Link to="/settings" className="underline">
                  Settings
                </Link>{" "}
                to choose who gets this playlist.
              </li>
            ) : (
              <li
                className="text-muted-foreground flex items-center justify-between gap-3 text-sm"
                role="alert"
              >
                Couldn&rsquo;t load Plex accounts.
                <Button variant="outline" size="sm" onClick={() => void plexUsers.refetch()}>
                  Retry
                </Button>
              </li>
            )
          ) : (
            (plexUsers.data?.users ?? []).map((user) => {
              const checked = targetIds.has(user.id);
              const state = playlist.plex?.[user.id];
              return (
                <li key={user.id} className="flex items-center justify-between gap-3 text-sm">
                  <label className="flex items-center gap-2">
                    <Checkbox
                      checked={checked}
                      onCheckedChange={() => toggleTarget(user)}
                      aria-label={user.name}
                    />
                    <span className="font-medium">{user.name}</span>
                  </label>
                  {/* Show a status for every checked target — even before its
                      first sync (state undefined → "Not synced yet"), matching
                      the admin row which always shows one. */}
                  {checked && (
                    <StatusLine status={syncStatus(state, playlist, "Not synced yet")} />
                  )}
                </li>
              );
            })
          )}
        </ul>
      </section>

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
