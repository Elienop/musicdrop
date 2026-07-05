import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";

import {
  type PlaylistDetail,
  type PlaylistTrack,
  useDeletePlaylist,
  usePlaylist,
  useRemoveEntry,
  useRenamePlaylist,
  useReorderTracks,
  useResolveEntry,
  useSetTargets,
  useSyncPlaylist,
} from "@/api/usePlaylists";
import { usePlexUsers } from "@/api/usePlex";
import { BackLink } from "@/components/albums/album-grid";
import { TrackMatchPicker } from "@/components/playlists/TrackMatchPicker";
import {
  Close,
  Edit,
  Error as ErrorIcon,
  MoveDown,
  MoveUp,
  MusicFallback,
  Remove,
  Refresh,
  Spinner,
  Success,
  Warning,
} from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { SectionLabel } from "@/components/system/SectionLabel";
import { StatusBanner } from "@/components/system/StatusBanner";
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
import { useDeferredH1Focus } from "@/lib/useDeferredH1Focus";
import { useFocusAfterMutation } from "@/lib/useFocusAfterMutation";

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

/** Render a sync status with a leading concept icon + semantic color. The text
 * label is always present (the color/icon are emphasis, not the only signal). */
function StatusLine({ status }: { status: SyncStatus }) {
  const { label, tone } = status;
  const Icon =
    tone === "success"
      ? Success
      : tone === "warning"
        ? Warning
        : tone === "destructive"
          ? ErrorIcon
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
  // Cold-load focus repair (see useDeferredH1Focus).
  useDeferredH1Focus(!isPending && !isError);

  if (isPending) {
    return (
      <PageSkeleton announce="Loading playlist…">
        <DetailSkeleton />
      </PageSkeleton>
    );
  }
  if (isError) {
    return (
      <div className="flex flex-col gap-6">
        <BackLink to="/playlists" label="Playlists" />
        <ErrorState
          message="Couldn’t load this playlist"
          onRetry={() => void refetch()}
        />
      </div>
    );
  }
  return <PlaylistDetailView playlist={data} />;
}

function PlaylistDetailView({ playlist }: { playlist: PlaylistDetail }) {
  const navigate = useNavigate();
  const rename = useRenamePlaylist(playlist.id);
  const remove = useDeletePlaylist();
  const reorder = useReorderTracks(playlist.id);
  const removeEntry = useRemoveEntry(playlist.id);
  const resolve = useResolveEntry(playlist.id);
  const sync = useSyncPlaylist(playlist.id);
  const plexUsers = usePlexUsers();
  const setTargets = useSetTargets(playlist.id);

  const [editingName, setEditingName] = useState(false);
  const [draftName, setDraftName] = useState(playlist.name);

  // The one pending entry whose match picker is open (uid), or null. A single
  // shared picker is driven off this — clicking a row's "Match…" arms it, the
  // pick resolves that uid and disarms.
  const [matchUid, setMatchUid] = useState<string | null>(null);

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

  // Single polite live region for reorder/remove announcements (kept alongside
  // the sonner toasts — the toast layer is the sighted-user channel, this
  // region is the assistive-tech one).
  const [statusMsg, setStatusMsg] = useState("");

  // Focus restoration for the mutating tracklist — the shared hook generalizes
  // the page's old pendingFocus engine. Controls register as
  // `${trackId}:up|down|remove`; the empty state registers as "empty".
  const { register, requestFocus } = useFocusAfterMutation();

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
    const message = `Moved ${displayTitle(moved)} to position ${target + 1}`;
    setStatusMsg(message);
    const dirKey = dir === -1 ? "up" : "down";
    const altKey = dir === -1 ? "down" : "up";
    requestFocus(`${moved.uid}:${dirKey}`, `${moved.uid}:${altKey}`);

    reorder.mutate(
      next.map((t) => t.uid),
      {
        onSuccess: () => toast.success(message),
        onError: () => setTracks(prev),
      },
    );
  }

  /** Resolve the armed pending entry to the picked library track, keeping its
   * position. On success the mutation swaps the detail cache (which reseeds the
   * local tracklist); the picker disarms either way. */
  function handlePick(itemId: number) {
    const entryUid = matchUid;
    if (entryUid === null) {
      return;
    }
    resolve.mutate(
      { entryUid, itemId },
      {
        onSuccess: () => setStatusMsg("Matched the track to your library"),
      },
    );
    setMatchUid(null);
  }

  /** Remove the track at `index`. On success announce + toast it and move
   * focus to a surviving sibling (the row that shifts up into its slot, else
   * the previous row, else the empty state). The local `setTracks` runs in the
   * SAME handler as `requestFocus` so the commit the hook fulfils the request
   * on already has the survivor list / empty state mounted (the refetch reseed
   * lands later and is a no-op for focus). */
  function handleRemove(index: number) {
    const removed = tracks[index];
    const afterRemoval = tracks.filter((_, i) => i !== index);
    removeEntry.mutate(removed.uid, {
      onSuccess: () => {
        const message = `Removed ${displayTitle(removed)}`;
        setStatusMsg(message);
        toast.success(message);
        setTracks(afterRemoval);
        if (afterRemoval.length === 0) {
          requestFocus("empty");
        } else {
          const survivor = afterRemoval[Math.min(index, afterRemoval.length - 1)];
          requestFocus(`${survivor.uid}:remove`);
        }
      },
    });
  }

  // Count unmatched (pending) rows from the local tracklist so the header stays
  // in step with optimistic add/remove/resolve edits, not the last server body.
  const unmatchedCount = tracks.filter((t) => t.pending).length;

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
                <Success className="size-4" aria-hidden="true" />
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
                <Close className="size-4" aria-hidden="true" />
              </Button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              {/* THE page h1 (detail-page rule: dynamic titles render their own
                  h1 at the page scale instead of PageHeader; tabIndex -1 keeps
                  RouteAnnouncer's focus contract). */}
              <h1
                tabIndex={-1}
                className="font-display text-display truncate font-semibold tracking-tight"
                title={playlist.name}
              >
                {playlist.name}
              </h1>
              <Button
                size="icon-sm"
                variant="ghost"
                onClick={() => {
                  setDraftName(playlist.name);
                  setEditingName(true);
                }}
                aria-label="Edit name"
              >
                <Edit className="size-4" aria-hidden="true" />
              </Button>
            </div>
          )}
          <p className="text-muted-foreground text-sm tabular-nums">
            {tracks.length} {tracks.length === 1 ? "track" : "tracks"}
            {unmatchedCount > 0 ? ` · ${unmatchedCount} unmatched` : ""}
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
                <Spinner className="size-4 animate-spin" aria-hidden="true" />
                Syncing&hellip;
              </>
            ) : (
              <>
                <Refresh className="size-4" aria-hidden="true" /> Sync to Plex
              </>
            )}
          </Button>

          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="outline" size="sm">
                <Remove className="size-4" aria-hidden="true" /> Delete playlist
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
                      <Spinner className="size-4 animate-spin" aria-hidden="true" />
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

      {/* Error stack — rename stays form-adjacent inline text; the list/sync
          mutation failures ride StatusBanner (tone=destructive ⇒ role=alert). */}
      {rename.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t rename the playlist. Try again.
        </p>
      )}
      {reorder.isError && (
        <StatusBanner tone="destructive" icon={ErrorIcon}>
          Couldn&rsquo;t save the new order. Try again.
        </StatusBanner>
      )}
      {removeEntry.isError && (
        <StatusBanner tone="destructive" icon={ErrorIcon}>
          Couldn&rsquo;t remove the track. Try again.
        </StatusBanner>
      )}
      {resolve.isError && (
        <StatusBanner tone="destructive" icon={ErrorIcon}>
          Couldn&rsquo;t match the track. Try again.
        </StatusBanner>
      )}
      {sync.isError &&
        (isPlexNotConfigured(sync.error) ? (
          <StatusBanner tone="destructive" icon={ErrorIcon}>
            Connect Plex in{" "}
            <Link to="/settings/integrations" className="focus-ring rounded-sm underline">
              Settings
            </Link>{" "}
            first.
          </StatusBanner>
        ) : (
          <StatusBanner tone="destructive" icon={ErrorIcon}>
            Couldn&rsquo;t sync to Plex. Try again.
          </StatusBanner>
        ))}
      {setTargets.isError && (
        <StatusBanner tone="destructive" icon={ErrorIcon}>
          Couldn&rsquo;t save the Plex targets. Try again.
        </StatusBanner>
      )}

      <section
        className="flex flex-col gap-2 rounded-xl border p-4"
        aria-label="Plex sync"
      >
        <SectionLabel>Plex sync</SectionLabel>
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
                <Link to="/settings/integrations" className="focus-ring rounded-sm underline">
                  Settings
                </Link>{" "}
                to choose who gets this playlist.
              </li>
            ) : (
              <li>
                <ErrorState
                  variant="inline"
                  message="Couldn’t load Plex accounts."
                  onRetry={() => void plexUsers.refetch()}
                />
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
        <EmptyTracks register={register} />
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
                key={track.uid}
                track={track}
                position={index + 1}
                isFirst={index === 0}
                isLast={index === tracks.length - 1}
                onMoveUp={() => move(index, -1)}
                onMoveDown={() => move(index, 1)}
                onRemove={() => handleRemove(index)}
                onMatch={() => setMatchUid(track.uid)}
                registerRef={register}
              />
            ))}
          </TableBody>
        </Table>
      )}

      {/* One shared picker for whichever pending row is armed (matchUid). It is
          playlist-agnostic — it just hands back a library item id, which
          handlePick resolves onto the armed entry. */}
      <TrackMatchPicker
        open={matchUid !== null}
        onOpenChange={(next) => {
          if (!next) setMatchUid(null);
        }}
        onPick={(picked) => handlePick(picked.item_id)}
        title="Match to a library track"
      />
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
  onMatch,
  registerRef,
}: {
  track: PlaylistTrack;
  position: number;
  isFirst: boolean;
  isLast: boolean;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
  onMatch: () => void;
  registerRef: (key: string, el: HTMLButtonElement | null) => void;
}) {
  // A pending row (an import that didn't match a library track) keeps its slot
  // with remembered metadata and offers a "Match…" action. A resolved row whose
  // beets item no longer resolves keeps its slot too, greyed and labelled. Both
  // read as "not a live library track", so both are dimmed; the metadata line is
  // shown for the live row and the pending one (it's all we know), but not for a
  // vanished resolved track (there's nothing left to show).
  const title = displayTitle(track);
  const showMeta = track.available || track.pending;
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
            {track.pending ? (
              <Badge variant="secondary" className="shrink-0 text-xs font-normal">
                Pending
              </Badge>
            ) : (
              !track.available && (
                <Badge variant="outline" className="shrink-0 text-xs font-normal">
                  unavailable
                </Badge>
              )
            )}
          </div>
          {showMeta && (
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
          {track.pending && (
            <Button
              size="sm"
              variant="outline"
              className="mr-1"
              onClick={onMatch}
              aria-label={`Match ${title}`}
            >
              Match&hellip;
            </Button>
          )}
          <Button
            ref={(el) => registerRef(`${track.uid}:up`, el)}
            size="icon-sm"
            variant="ghost"
            onClick={onMoveUp}
            disabled={isFirst}
            aria-label={`Move ${title} up`}
          >
            <MoveUp className="size-4" aria-hidden="true" />
          </Button>
          <Button
            ref={(el) => registerRef(`${track.uid}:down`, el)}
            size="icon-sm"
            variant="ghost"
            onClick={onMoveDown}
            disabled={isLast}
            aria-label={`Move ${title} down`}
          >
            <MoveDown className="size-4" aria-hidden="true" />
          </Button>
          <Button
            ref={(el) => registerRef(`${track.uid}:remove`, el)}
            size="icon-sm"
            variant="ghost"
            onClick={onRemove}
            aria-label={`Remove ${title}`}
          >
            <Remove className="size-4" aria-hidden="true" />
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
}

function EmptyTracks({
  register,
}: {
  register: (key: string, el: HTMLElement | null) => void;
}) {
  return (
    // The wrapper (not the inner copy) is the focus target the remove-last-
    // track flow lands on — registered under the "empty" key so the shared
    // focus hook reaches it like any row control.
    <div
      tabIndex={-1}
      ref={(el) => register("empty", el)}
      className="focus-ring rounded-xl"
    >
      <EmptyState
        icon={MusicFallback}
        title="No tracks yet"
        body="Add some from an album or search."
        bordered
      />
    </div>
  );
}

/** Bones only — PageSkeleton owns the aria-hidden + the loading announcement. */
function DetailSkeleton() {
  return (
    <div className="flex flex-col gap-6">
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
