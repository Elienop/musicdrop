import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";

import {
  type PlaylistDetail,
  type PlaylistMergeResult,
  type PlaylistTrack,
  useDeletePlaylist,
  useDeletePlaylistArtwork,
  usePlaylist,
  useRemoveEntry,
  useRenamePlaylist,
  useReorderTracks,
  useResolveEntry,
  useSetTargets,
  useSyncPlaylist,
  useUploadPlaylistArtwork,
} from "@/api/usePlaylists";
import { usePlexUsers } from "@/api/usePlex";
import { BackLink } from "@/components/albums/album-grid";
import { MergePlaylistDialog } from "@/components/playlists/MergePlaylistDialog";
import { PlaylistCover } from "@/components/playlists/PlaylistCover";
import { TrackMatchPicker } from "@/components/playlists/TrackMatchPicker";
import {
  Close,
  Cover,
  Edit,
  Error as ErrorIcon,
  Merge,
  MoveDown,
  MoveUp,
  MusicFallback,
  Remove,
  Refresh,
  Spinner,
  Success,
  Upload,
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

/** Why Plex couldn't place one track on the last sync. */
type PlexMissReason = PlexTargetState["missing_tracks"][number]["reason"];

/** The tooltip for each miss reason — they're different remedies (nothing
 * matched at all vs. several matched and we refuse to guess), so each row says
 * which one it hit rather than a generic "not found". */
const MISS_TITLES: Record<PlexMissReason, string> = {
  not_found:
    "Plex has no track with this file, and nothing matched by artist and title. Check the file is in your Plex library, then sync again.",
  ambiguous:
    "Several Plex tracks share this artist and title, and none has this file, so MusicDrop won't guess which one. Sort out the copies in Plex, then sync again.",
};

/** item id -> why Plex couldn't place it on the last sync. Keyed by item id (not
 * row uid) because the resolve is per library track: two rows for one item are
 * both missing or both found. The admin target's list is THE list — one resolve
 * against the (server-global) ratingKeys serves every fan-out target. */
function plexMissesByItem(playlist: PlaylistDetail): Map<number, PlexMissReason> {
  const out = new Map<number, PlexMissReason>();
  for (const miss of playlist.plex?.admin?.missing_tracks ?? []) {
    out.set(miss.item_id, miss.reason);
  }
  return out;
}

/** How the last sync FOUND each track it placed on Plex, in the three ways the
 * server tallies (`PlexMatchCounts`). Split out of the wire shape so the copy
 * below reads one flat object and never has to know the field names.
 *
 * `total` is the number of tracks the sync actually resolved — always the sum,
 * never the playlist length, so the sentence built from it stays true when some
 * rows are missing from Plex (those are the separate "N not in Plex" count). */
interface MatchBreakdown {
  total: number;
  path: number;
  artistTitle: number;
  albumLength: number;
}

/** The `admin` target's tally, or null when there is nothing to say.
 *
 * ONE tally per sync, not one per target: the server resolves the library once
 * and reuses that resolution for every account (ratingKeys are server-global),
 * so `admin` carries THE numbers and repeating them under each fan-out user
 * would imply each account was looked up separately. This is the same rule the
 * per-row miss markers follow (`plexMissesByItem`).
 *
 * All-zero means "nothing to say", NEVER "zero matched by file": a record
 * written before the server started tallying has no counts, and a target whose
 * push failed deliberately zeroes them even when the resolution succeeded.
 * Reading zero as evidence would invent the exact false alarm this surface
 * exists to avoid — so an empty sum returns null and renders nothing. */
function matchBreakdown(playlist: PlaylistDetail): MatchBreakdown | null {
  const counts = playlist.plex?.admin?.matched_by;
  if (!counts) {
    return null;
  }
  const total = counts.path + counts.artist_title + counts.album_length;
  return total === 0
    ? null
    : {
        total,
        path: counts.path,
        artistTitle: counts.artist_title,
        albumLength: counts.album_length,
      };
}

/** True when at least one track was found by something weaker than its file —
 * the only condition under which this surface says anything beyond the count. */
function hasWeakMatches(breakdown: MatchBreakdown): boolean {
  return breakdown.path < breakdown.total;
}

/** The counts, as one sentence a Plex user can read without knowing how
 * MusicDrop matches. "by file" is the strong rung; the other two are named for
 * what they compared, not for their internal names.
 *
 * A zero file count is stated OUT LOUD ("none by file") rather than dropped
 * from the list: the absence of that clause is precisely what went unnoticed
 * for years, and a reader cannot notice a clause that was never printed. */
function matchCountsSentence(breakdown: MatchBreakdown): string {
  const tracks = `${breakdown.total} ${breakdown.total === 1 ? "track" : "tracks"}`;
  const weaker: string[] = [];
  if (breakdown.artistTitle > 0) {
    weaker.push(`${breakdown.artistTitle} by artist and title`);
  }
  if (breakdown.albumLength > 0) {
    weaker.push(`${breakdown.albumLength} by album and length`);
  }
  if (weaker.length === 0) {
    return `Matched ${tracks} by file.`;
  }
  const byFile = breakdown.path === 0 ? "none by file" : `${breakdown.path} by file`;
  return `Matched ${tracks}: ${byFile}, ${weaker.join(", ")}.`;
}

/** What a weaker match risks, in the user's terms: a tag match can land on a
 * different copy of the track than the file they meant. */
const WEAK_MATCH_NOTE =
  "The rest were matched on their tags rather than their files, which can land on a different copy of a track.";

/** The announcement appended to the post-sync live-region message — "" on a
 * healthy sync, so the common case stays exactly as quiet as it was.
 *
 * Deliberately terser than the visible copy: a live region is read aloud in one
 * go, so it flags the problem and names where to fix it instead of reciting the
 * whole explanation the section already shows on screen. */
function matchAnnouncement(breakdown: MatchBreakdown | null): string {
  if (breakdown === null || !hasWeakMatches(breakdown)) {
    return "";
  }
  const counts = matchCountsSentence(breakdown);
  return breakdown.path === 0
    ? `${counts} Nothing matched by file — check the Plex library path in Settings.`
    : `${counts} ${WEAK_MATCH_NOTE}`;
}

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
    return { label: "Out of date; re-sync", tone: "warning" };
  }
  switch (state.status) {
    case "ok":
      return { label: "Synced", tone: "success" };
    case "partial": {
      const marked = state.missing_tracks.length;
      if (marked >= state.missing) {
        return { label: `${state.missing} not in Plex`, tone: "warning" };
      }
      // Fewer carried identities than misses — the server caps the list
      // (MISSING_TRACKS_CAP). Only those rows can wear a badge, so say which
      // ones the badges cover; otherwise the unmarked remainder reads as fine.
      // `marked === 0` can't come from the cap (it truncates to 200, never 0):
      // it means the state predates the identities, which one re-sync fixes.
      return {
        label:
          marked === 0
            ? `${state.missing} not in Plex; re-sync to see which`
            : `${state.missing} not in Plex; first ${marked} marked`,
        tone: "warning",
      };
    }
    case "empty":
      // Nothing resolved, so the sync touched nothing. Say which case this is —
      // and both are a warning: the user asked for a push and got none, so the
      // no-copy-at-all outcome must not read quieter than the milder one.
      // The exception is a playlist with nothing to send in the first place
      // (empty, or only pending rows): missing === 0, nothing went wrong, so no
      // alarm is earned.
      if (state.rating_key) {
        return { label: "No matching tracks; Plex copy left as is", tone: "warning" };
      }
      return state.missing === 0
        ? { label: "Nothing to sync", tone: "muted" }
        : { label: "No matching tracks; nothing sent to Plex", tone: "warning" };
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

/** How the last sync found its tracks — the footnote of the Plex sync section.
 *
 * Three tiers, and the tiering is the whole design:
 *  - nothing recorded (or a failed target) — renders nothing at all;
 *  - everything found by file, the normal case — one muted line, no icon, no
 *    color, at the section's footnote size. Present so the user has a baseline
 *    to notice a change AGAINST, quiet enough not to read as news;
 *  - not one track found by file — a warning box naming the likely cause. That
 *    is the failure this whole surface exists for: it means every path lookup
 *    missed, which for a whole playlist is a wrong `library_path`, not luck.
 *    A partial (some by file, some by tags) stays in the muted tier: a handful
 *    of tracks Plex holds under a different copy is ordinary, so escalating it
 *    would train the user to ignore the box that matters. */
function MatchSummary({ playlist }: { playlist: PlaylistDetail }) {
  const breakdown = matchBreakdown(playlist);
  if (breakdown === null) {
    return null;
  }
  return (
    <div className="flex flex-col gap-1 text-xs">
      <p className="text-muted-foreground">{matchCountsSentence(breakdown)}</p>
      {breakdown.path === 0 ? (
        // The settings panel's inline-warning idiom: the icon is decoration,
        // the sentence carries the meaning, so nothing here is color-only.
        <p className="border-warning/50 bg-warning/10 text-foreground flex items-start gap-2 rounded-md border p-2">
          <Warning className="text-warning mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>
            Nothing matched by file. That usually means the Plex library path in{" "}
            <Link to="/settings/integrations" className="focus-ring rounded-sm underline">
              Settings
            </Link>{" "}
            doesn’t point where Plex keeps your music, so MusicDrop fell back to matching
            on tags — which can land on a different copy of a track.
          </span>
        </p>
      ) : (
        hasWeakMatches(breakdown) && <p className="text-muted-foreground">{WEAK_MATCH_NOTE}</p>
      )}
    </div>
  );
}

/** A first-time sync fails with a 409 when Plex isn't connected yet (no base
 * URL / token). `useSyncPlaylist` tags the thrown error with the HTTP status so
 * we can point the user at Settings instead of showing the generic retry copy. */
function isPlexNotConfigured(err: unknown): boolean {
  return err instanceof Error && (err as { status?: number }).status === 409;
}

/** Debounce window for coalescing a rapid burst of Plex target toggles into one
 * save (so a flurry of checkbox clicks collapses to a single PATCH). */
const TARGET_SAVE_DEBOUNCE_MS = 350;

/** Swap the row identified by `uid` with its neighbor in `dir` (-1 up / +1
 * down), keyed by uid rather than a captured index so it composes with a
 * background reseed that landed between the click and the state update. A no-op
 * when the row or its neighbor is no longer present. */
function swapByUid(list: PlaylistTrack[], uid: string, dir: -1 | 1): PlaylistTrack[] {
  const i = list.findIndex((t) => t.uid === uid);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= list.length) {
    return list;
  }
  const next = [...list];
  [next[i], next[j]] = [next[j], next[i]];
  return next;
}

/** Set equality by membership (order-independent) — tells whether the persisted
 * target set has caught up with the latest desired one. */
function sameSet(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) {
    return false;
  }
  for (const value of a) {
    if (!b.has(value)) {
      return false;
    }
  }
  return true;
}

/** The merge outcome as one announceable sentence, built from the SERVER's
 * counts. The client never recounts: the response is the authority on what
 * actually happened, and merge deliberately leaves Plex alone, so the sentence
 * ends by asking for the sync the "Out of date" status is about to demand. */
function mergeSummary(result: PlaylistMergeResult, sourceName: string): string {
  const added = `${result.added} ${result.added === 1 ? "track" : "tracks"}`;
  const skipped =
    result.skipped_duplicates > 0 ? `, skipped ${result.skipped_duplicates} already here` : "";
  const deleted = result.source_deleted ? `, and deleted ${sourceName}` : "";
  return `Merged ${sourceName}: added ${added}${skipped}${deleted}. Sync to Plex to push the change.`;
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

  // Inline "Edit artwork" disclosure (album CoverEditPanel idiom): opening moves
  // focus into the panel; closing from the toggle returns focus to it.
  const [editingArtwork, setEditingArtwork] = useState(false);
  const artworkPanelRef = useRef<HTMLDivElement>(null);
  const artworkToggleRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (editingArtwork) artworkPanelRef.current?.focus();
  }, [editingArtwork]);
  const closeArtworkPanel = () => {
    setEditingArtwork(false);
    artworkToggleRef.current?.focus();
  };

  // The one pending entry whose match picker is open (uid), or null. A single
  // shared picker is driven off this — clicking a row's "Match…" arms it, the
  // pick resolves that uid and disarms.
  const [matchUid, setMatchUid] = useState<string | null>(null);

  // The merge dialog. Mounted only while open - nothing here needs it before
  // then. Its own usePlaylists() is disabled while closed as well, so neither
  // path can fire GET /api/playlists off a page that merely CAN merge.
  const [mergeOpen, setMergeOpen] = useState(false);

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
  // keyboard focus and block every other checkbox.
  const [targetIds, setTargetIds] = useState(() => new Set(playlist.target_plex_users));
  // The latest DESIRED set (updated synchronously on every toggle) plus the
  // plumbing that guarantees at most ONE target PATCH is ever in flight, always
  // carrying the final set. Without this, a rapid A-then-B burst fires two
  // concurrent full-list PATCHes ([A] then [A,B]); if the backend settles them
  // out of order the server lands on a subset and the settle-refetch reseeds the
  // checkboxes to it — silently dropping the target the user just enabled.
  const desiredTargets = useRef(new Set(playlist.target_plex_users));
  const targetsDirty = useRef(false); // a change is scheduled or mid-save
  const targetsInFlight = useRef(false); // a PATCH is currently running
  const targetsTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Re-seed the optimistic set from the server ONLY when we've no change of our
  // own outstanding. Otherwise a settle/refetch that lands a stale subset would
  // revert a checkbox the user just toggled (the dropped-target bug). Once our
  // save is fully persisted (targetsDirty cleared) the next refetch reseeds.
  useEffect(() => {
    if (targetsDirty.current) {
      return;
    }
    const fromServer = new Set(playlist.target_plex_users);
    desiredTargets.current = fromServer;
    setTargetIds(fromServer);
  }, [playlist.target_plex_users]);

  // Drop any pending debounce on unmount.
  useEffect(() => {
    return () => {
      if (targetsTimer.current) {
        clearTimeout(targetsTimer.current);
      }
    };
  }, []);

  // Single polite live region for reorder/remove announcements (kept alongside
  // the sonner toasts — the toast layer is the sighted-user channel, this
  // region is the assistive-tech one).
  const [statusMsg, setStatusMsg] = useState("");

  // Focus restoration for the mutating tracklist — the shared hook generalizes
  // the page's old pendingFocus engine. Controls register as
  // `${trackId}:up|down|remove`; the empty state registers as "empty".
  const { register, requestFocus } = useFocusAfterMutation();

  // WHY (perf): PlaylistTrackRow is React.memo'd so a header-only state change
  // (rename keystroke, statusMsg live-region update, target toggle, artwork
  // panel) doesn't reconcile the whole tracklist — which can run to a few
  // thousand rows, each three icon Buttons. memo only holds if every callback a
  // row receives keeps a STABLE identity across a parent re-render, so we route
  // all row actions through ONE ref of the volatile deps (the live tracklist +
  // the mutations), refreshed each render, and hand the rows truly-stable
  // useCallback([]) handlers that read it. A rename keystroke then changes NO
  // row prop, so not one row re-renders.
  const rowDeps = useRef({ tracks, reorder, removeEntry, requestFocus });
  rowDeps.current = { tracks, reorder, removeEntry, requestFocus };

  function saveName() {
    const next = draftName.trim();
    if (next.length === 0 || next === playlist.name) {
      setEditingName(false);
      return;
    }
    rename.mutate({ name: next }, { onSuccess: () => setEditingName(false) });
  }

  /** Persist the latest desired target set. Single-flight: if a PATCH is already
   * running we do nothing (this re-fires on settle); if the desired set moved on
   * while the PATCH was in flight we save again until the server matches it — so
   * the last write always wins regardless of completion order. */
  function flushTargets() {
    if (targetsInFlight.current) {
      return;
    }
    const sending = [...desiredTargets.current];
    targetsInFlight.current = true;
    setTargets.mutate(sending, {
      onSettled: () => {
        targetsInFlight.current = false;
        if (sameSet(desiredTargets.current, new Set(sending))) {
          targetsDirty.current = false; // server caught up — reseed may resume
        } else {
          flushTargets(); // desired moved on mid-flight — converge with another save
        }
      },
    });
  }

  /** Debounce the save so a burst of toggles collapses into one PATCH carrying
   * the final set. Marks the target set dirty up front so the reseed effect
   * won't clobber the in-progress change. */
  function scheduleTargetSave() {
    targetsDirty.current = true;
    if (targetsTimer.current) {
      clearTimeout(targetsTimer.current);
    }
    targetsTimer.current = setTimeout(() => {
      targetsTimer.current = null;
      flushTargets();
    }, TARGET_SAVE_DEBOUNCE_MS);
  }

  /** Add/remove a Plex Home user from this playlist's fan-out targets. Updates
   * the optimistic set + latest-desired ref immediately, then schedules a
   * debounced, single-flight PATCH of the new full list. Announces the specific
   * action (so consecutive saves re-announce) via the live region. A newly-added
   * account reads "Not synced yet" until the next sync pushes its copy. */
  function toggleTarget(user: { id: string; name: string }) {
    const checked = desiredTargets.current.has(user.id);
    const next = new Set(desiredTargets.current);
    if (checked) {
      next.delete(user.id);
    } else {
      next.add(user.id);
    }
    desiredTargets.current = next;
    setTargetIds(next);
    setStatusMsg(
      `${checked ? "Removed" : "Added"} ${user.name} ${checked ? "from" : "to"} Plex sync`,
    );
    scheduleTargetSave();
  }

  /** Move the row identified by `uid` one slot in `dir`: reorder locally for
   * instant feedback, PUT the new full order, and refocus the moved row's move
   * button (falling back to its sibling button when the move lands it at an
   * end). Stable identity (useCallback []) so it doesn't defeat the row memo;
   * reads the live tracklist/mutation off `rowDeps` and derives position from
   * `uid` rather than a captured index, so it never closes over the array. */
  const onMove = useCallback((uid: string, dir: -1 | 1) => {
    const { tracks, reorder, requestFocus } = rowDeps.current;
    const index = tracks.findIndex((t) => t.uid === uid);
    const target = index + dir;
    if (index < 0 || target < 0 || target >= tracks.length) {
      return;
    }
    const moved = tracks[index];
    const message = `Moved ${displayTitle(moved)} to position ${target + 1}`;
    setStatusMsg(message);
    const dirKey = dir === -1 ? "up" : "down";
    const altKey = dir === -1 ? "down" : "up";
    requestFocus(`${moved.uid}:${dirKey}`, `${moved.uid}:${altKey}`);

    // Derive the optimistic order AND the PUT body from one snapshot (this
    // render's tracklist, read via the ref) so they can't diverge. A move is a
    // single deliberate action, so the click-time order is the intent; a
    // background reseed that lands between render and click self-heals on the
    // next refetch. (The rapid-edit resurrection guard that must stay uid-keyed
    // lives on the remove path, not here.)
    const prev = tracks;
    const next = swapByUid(tracks, moved.uid, dir);
    setTracks(next);
    reorder.mutate(
      next.map((t) => t.uid),
      {
        onSuccess: () => toast.success(message),
        onError: () => setTracks(prev),
      },
    );
  }, []);

  // The row the picker is armed for, and whether picking will REPLACE a live
  // track rather than fill an empty slot. An unavailable row has an item id but
  // nothing behind it, so it is not a replacement either - only a row that is
  // resolved AND present is.
  const armed = matchUid === null ? null : (tracks.find((t) => t.uid === matchUid) ?? null);
  const armedReplaces = armed !== null && !armed.pending && armed.available;

  /** Point the armed entry at the picked library track, keeping its position.
   * On success the mutation swaps the detail cache (which reseeds the local
   * tracklist); the picker disarms either way. */
  function handlePick(itemId: number) {
    const entryUid = matchUid;
    if (entryUid === null) {
      return;
    }
    const replaces = armedReplaces;
    resolve.mutate(
      { entryUid, itemId },
      {
        onSuccess: () =>
          setStatusMsg(
            replaces
              ? "Replaced the track; the row kept its position"
              : "Matched the track to your library",
          ),
      },
    );
    setMatchUid(null);
  }

  /** Arm the shared match picker for row `uid` — any row that needs attention,
   * not just a pending one: a pending import, a row whose beets item is gone,
   * or a resolved row the last Plex sync could not place. Stable identity so it
   * doesn't defeat the row memo. */
  const onMatch = useCallback((uid: string) => setMatchUid(uid), []);

  /** Remove the row identified by `uid`. On success announce + toast it and
   * move focus to a surviving sibling (the row that shifts up into its slot,
   * else the previous row, else the empty state). The local `setTracks` runs in
   * the SAME handler as `requestFocus` so the commit the hook fulfils the
   * request on already has the survivor list / empty state mounted (the refetch
   * reseed lands later and is a no-op for focus). Stable identity (useCallback
   * []) and uid-keyed (reads the live tracklist off `rowDeps`) so it neither
   * defeats the row memo nor closes over a captured index/array. */
  const onRemove = useCallback((uid: string) => {
    const { tracks, removeEntry } = rowDeps.current;
    const removed = tracks.find((t) => t.uid === uid);
    if (!removed) {
      return;
    }
    removeEntry.mutate(removed.uid, {
      onSuccess: () => {
        const message = `Removed ${displayTitle(removed)}`;
        setStatusMsg(message);
        toast.success(message);
        // Drop THIS uid from the LATEST state AND choose the focus target from
        // that same post-removal list. Two rapid removes each remove their own
        // row, so neither resurrects the other's; and because the empty/survivor
        // decision reads the post-removal length, two removes-to-empty let the
        // last one see length 0 and land on the empty state (a decision off the
        // stale render list would leave neither seeing 0). requestFocus only
        // stores the key, so it's safe to call from the updater.
        setTracks((cur) => {
          const at = cur.findIndex((t) => t.uid === removed.uid);
          if (at < 0) {
            return cur; // already gone (e.g. a double-fire) — leave focus be
          }
          const nextList = cur.filter((t) => t.uid !== removed.uid);
          if (nextList.length === 0) {
            requestFocus("empty");
          } else {
            requestFocus(`${nextList[Math.min(at, nextList.length - 1)].uid}:remove`);
          }
          return nextList;
        });
      },
    });
  }, []);

  // Count unmatched (pending) rows from the local tracklist so the header stays
  // in step with optimistic add/remove/resolve edits, not the last server body.
  const unmatchedCount = tracks.filter((t) => t.pending).length;

  // Which library items the last sync couldn't place on Plex. Memoized on the
  // server body so a header-only re-render hands every row the SAME primitive
  // (usually undefined) and the row memo keeps holding.
  const plexMisses = useMemo(() => plexMissesByItem(playlist), [playlist]);

  // How many Plex copies actually exist for this playlist — targets whose sync
  // recorded a rating_key. Drives the delete-dialog's honest "…N synced Plex
  // copies…" line (a failed/empty target has a slot but no copy on Plex).
  const syncedPlexCopies = Object.values(playlist.plex ?? {}).filter(
    (state) => state.rating_key,
  ).length;

  return (
    <section className="flex flex-col gap-6" aria-label="Playlist">
      <BackLink to="/playlists" label="Playlists" />

      <p className="sr-only" role="status" aria-live="polite">
        {statusMsg}
      </p>

      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-4">
          <PlaylistCover
            playlist={playlist}
            className="border-border size-24 shrink-0 rounded-xl border"
          />
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
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            ref={artworkToggleRef}
            variant="outline"
            size="sm"
            onClick={() => setEditingArtwork((v) => !v)}
            aria-expanded={editingArtwork}
            aria-controls="playlist-artwork-panel"
          >
            <Cover className="size-4" aria-hidden="true" /> Edit artwork
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              sync.mutate(undefined, {
                // Announce the real outcome (synced / N not in Plex / no
                // matching tracks) instead of a blanket "complete" — derived
                // from the same label the visible status line shows. A sync
                // that leaned on the weaker matching says so as well; one that
                // did not adds nothing, so the healthy case is announced in
                // exactly the words it always was.
                onSuccess: (updated) => {
                  const note = matchAnnouncement(matchBreakdown(updated));
                  setStatusMsg(
                    `Plex sync: ${adminSyncStatus(updated).label}${note ? `. ${note}` : ""}`,
                  );
                },
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

          <Button variant="outline" size="sm" onClick={() => setMergeOpen(true)}>
            <Merge className="size-4" aria-hidden="true" /> Merge another playlist into this
            one&hellip;
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
                  {/* Count only the copies that actually landed on Plex (have a
                      recorded rating_key) — a failed/empty target holds a plex
                      slot but has no copy to remove. Built as one string so the
                      sentence reads exactly, uninterrupted by interpolation. */}
                  {syncedPlexCopies > 0
                    ? ` Also removes its ${syncedPlexCopies} synced Plex ${
                        syncedPlexCopies === 1 ? "copy" : "copies"
                      } on Plex.`
                    : null}
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

      {editingArtwork && (
        <div
          id="playlist-artwork-panel"
          ref={artworkPanelRef}
          tabIndex={-1}
          className="outline-none"
        >
          <ArtworkEditPanel playlist={playlist} onClose={closeArtworkPanel} />
        </div>
      )}

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
        {/* Sits below the per-target list because it describes the one library
            lookup they all share, not any single account's push. */}
        <MatchSummary playlist={playlist} />
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
                onMove={onMove}
                onRemove={onRemove}
                onMatch={onMatch}
                registerRef={register}
                plexMiss={track.id != null ? plexMisses.get(track.id) : undefined}
              />
            ))}
          </TableBody>
        </Table>
      )}

      {mergeOpen && (
        <MergePlaylistDialog
          target={playlist}
          open
          onOpenChange={setMergeOpen}
          onMerged={(result, sourceName) => setStatusMsg(mergeSummary(result, sourceName))}
        />
      )}

      {/* One shared picker for whichever row is armed (matchUid). It is
          playlist-agnostic — it just hands back a library item id, which
          handlePick resolves onto the armed entry. The title and description
          say which of the two things the pick will do, because re-pointing a
          row that already has a track REPLACES it rather than adding one. */}
      <TrackMatchPicker
        open={matchUid !== null}
        onOpenChange={(next) => {
          if (!next) setMatchUid(null);
        }}
        onPick={(picked) => handlePick(picked.item_id)}
        title={armedReplaces ? "Point this row at another track" : "Match to a library track"}
        // Only the replacement wording is passed; the other case is the
        // picker's own default, so the sentence lives in exactly one place.
        description={
          armedReplaces
            ? "Search your library and pick the track this row should point to instead. It replaces the current track and keeps its position."
            : undefined
        }
      />
    </section>
  );
}

/** Inline artwork editor (album CoverEditPanel idiom, trimmed to what a
 * playlist needs): upload a custom JPEG/PNG cover, or remove it to fall back to
 * the album-cover collage. The upload endpoint takes raw bytes; the picker ships
 * the File straight through. A 415/413 surfaces the server's own reason. The
 * Plex "Out of date" signal follows the server-side `updated_at` bump on its own
 * — no extra staleness plumbing here. */
function ArtworkEditPanel({
  playlist,
  onClose,
}: {
  playlist: PlaylistDetail;
  onClose: () => void;
}) {
  const upload = useUploadPlaylistArtwork(playlist.id);
  const removeArtwork = useDeletePlaylistArtwork(playlist.id);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // What just succeeded, so the panel can confirm the outcome inline (cleared
  // when a new action starts).
  const [outcome, setOutcome] = useState<"uploaded" | "removed" | null>(null);
  const busy = upload.isPending || removeArtwork.isPending;

  return (
    <section
      aria-label="Edit artwork"
      className="flex flex-col gap-3 rounded-lg border p-4"
    >
      <p className="text-muted-foreground text-sm">
        Upload a custom cover (JPEG or PNG) for this playlist. It replaces the
        album-cover collage and is pushed to Plex on the next sync.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="secondary"
          onClick={() => fileInputRef.current?.click()}
          disabled={busy}
        >
          <Upload className="size-4" aria-hidden="true" />
          {upload.isPending ? "Uploading…" : "Upload an image…"}
        </Button>
        {playlist.artwork_hash && (
          <Button
            variant="outline"
            aria-label="Remove artwork"
            onClick={() => {
              setOutcome(null);
              removeArtwork.mutate(undefined, {
                onSuccess: () => setOutcome("removed"),
              });
            }}
            disabled={busy}
          >
            <Remove className="size-4" aria-hidden="true" />
            {removeArtwork.isPending ? "Removing…" : "Remove"}
          </Button>
        )}
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Close
        </Button>
      </div>
      {/* Hidden native picker the "Upload an image…" button opens — the
          shadcn-idiomatic way to style a file input. Uploads on pick (no
          preview step): the header cover reflects the result after refetch. */}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/jpeg,image/png"
        aria-label="Upload artwork image"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = ""; // allow re-picking the same file
          if (!file) return;
          setOutcome(null);
          upload.mutate(file, { onSuccess: () => setOutcome("uploaded") });
        }}
      />
      {upload.isError && (
        <StatusBanner tone="destructive" icon={ErrorIcon}>
          {upload.error.message}
        </StatusBanner>
      )}
      {removeArtwork.isError && (
        <StatusBanner tone="destructive" icon={ErrorIcon}>
          {removeArtwork.error.message}
        </StatusBanner>
      )}
      {outcome !== null && !busy && (
        <p
          role="status"
          className="text-muted-foreground inline-flex items-center gap-1 text-sm"
        >
          <Success className="text-success size-4 shrink-0" aria-hidden="true" />
          {outcome === "uploaded" ? "Artwork updated." : "Artwork removed."}
        </p>
      )}
    </section>
  );
}

// Memoized so a header-only re-render of PlaylistDetailView (rename keystroke,
// statusMsg live-region update, target toggle, artwork panel) doesn't reconcile
// every row of a potentially multi-thousand-row tracklist. Default shallow prop
// comparison suffices BECAUSE the parent hands stable useCallback([]) handlers
// (onMove/onRemove/onMatch) and passes only primitives otherwise (position,
// isFirst, isLast) plus the reseed-stable `track` object — so an unrelated
// header change touches no prop of an untouched row.
const PlaylistTrackRow = memo(function PlaylistTrackRow({
  track,
  position,
  isFirst,
  isLast,
  onMove,
  onRemove,
  onMatch,
  registerRef,
  plexMiss,
}: {
  track: PlaylistTrack;
  position: number;
  isFirst: boolean;
  isLast: boolean;
  onMove: (uid: string, dir: -1 | 1) => void;
  onRemove: (uid: string) => void;
  onMatch: (uid: string) => void;
  registerRef: (key: string, el: HTMLButtonElement | null) => void;
  /** Set when the last Plex sync couldn't place this row's library item — a
   * primitive so the memo above still short-circuits an unrelated re-render. */
  plexMiss?: PlexMissReason;
}) {
  // A pending row (an import that didn't match a library track) keeps its slot
  // with remembered metadata. A resolved row whose beets item no longer resolves
  // keeps its slot too, greyed and labelled. Both read as "not a live library
  // track", so both are dimmed; the metadata line is shown for the live row and
  // the pending one (it's all we know), but not for a vanished resolved track
  // (there's nothing left to show). Which rows offer "Match…" is a wider
  // question than dimming and is decided by `canMatch` below — a resolved row
  // can be perfectly live and still need the action.
  const title = displayTitle(track);
  const showMeta = track.available || track.pending;

  // Offer the re-point action on any row that is not cleanly playable AND
  // placed: a pending import, a row whose beets item is gone, or a resolved row
  // the last Plex sync could not put on the server. A healthy row keeps no
  // action - one identical button per row down a 61-track playlist is noise.
  // (`!track.available` already covers pending today; the explicit term keeps
  // the three states legible if `_pending_row` ever changes.)
  const canMatch = track.pending || !track.available || plexMiss !== undefined;
  return (
    <TableRow className={track.available ? undefined : "bg-muted/40"}>
      <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
        {position}
      </TableCell>
      <TableCell>
        {/* A pending row carries the original source text (m3u line / file path
            / "plex:<name>") as a tooltip — for a bare-path entry it's the only
            "it was this" identity. Resolved rows have no source (title omitted). */}
        <div className="flex min-w-0 flex-col" title={track.source ?? undefined}>
          {/* flex-wrap + a shrinkable title keep this cell's min-content width
              small: the badges are shrink-0, so without it a flagged row widens
              the Title column past a phone and the table's own overflow-x-auto
              scroller (ui/table.tsx) hides the row's actions off the right
              edge. Wrapping drops the badge under the title only when it must. */}
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span
              className={`min-w-0 truncate font-medium ${track.available ? "" : "text-muted-foreground italic"}`}
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
            {/* The row is in the library but the last sync couldn't put it on
                Plex (the "N not in Plex" count says how many, this says WHICH
                and why). The sr-only phrase is the announced carrier — a Badge
                renders a generic <span>, where `title`/`aria-label` are not
                reliably announced, so the badge is hidden from the tree and
                keeps `title` for sighted hover (the album page's idiom).
                Skipped when the beets item is gone: "not in Plex" is noise on
                top of "unavailable". */}
            {plexMiss && track.available && (
              <>
                <span className="sr-only">Not in Plex: {MISS_TITLES[plexMiss]}</span>
                <Badge
                  variant="outline"
                  aria-hidden="true"
                  className="border-warning text-warning shrink-0 text-xs font-normal"
                  title={MISS_TITLES[plexMiss]}
                >
                  Not in Plex
                </Badge>
              </>
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
          {canMatch && (
            <Button
              size="sm"
              variant="outline"
              className="mr-1"
              onClick={() => onMatch(track.uid)}
              aria-label={`Match ${title}`}
            >
              Match&hellip;
            </Button>
          )}
          <Button
            ref={(el) => registerRef(`${track.uid}:up`, el)}
            size="icon-sm"
            variant="ghost"
            onClick={() => onMove(track.uid, -1)}
            disabled={isFirst}
            aria-label={`Move ${title} up`}
          >
            <MoveUp className="size-4" aria-hidden="true" />
          </Button>
          <Button
            ref={(el) => registerRef(`${track.uid}:down`, el)}
            size="icon-sm"
            variant="ghost"
            onClick={() => onMove(track.uid, 1)}
            disabled={isLast}
            aria-label={`Move ${title} down`}
          >
            <MoveDown className="size-4" aria-hidden="true" />
          </Button>
          <Button
            ref={(el) => registerRef(`${track.uid}:remove`, el)}
            size="icon-sm"
            variant="ghost"
            onClick={() => onRemove(track.uid)}
            aria-label={`Remove ${title}`}
          >
            <Remove className="size-4" aria-hidden="true" />
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
});

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
