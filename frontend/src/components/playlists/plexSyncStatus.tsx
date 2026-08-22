import type { Playlist } from "@/api/usePlaylists";
import { Error as ErrorIcon, Success, Warning } from "@/components/icons";

/** Per-target Plex sync bookkeeping, as it appears on either the summary
 * (`Playlist` list rows) or the detail (`PlaylistDetail`) wire. The two carry
 * an identical `plex` shape, so this one alias serves both; the summary page
 * needs only `updated_at` + the target states, so it does not depend on the
 * heavier detail type. */
type PlexTargetState = Playlist["plex"][string];

/** A playlist as seen for sync status — both `Playlist` (list) and
 * `PlaylistDetail` satisfy this. Only `updated_at` is needed to judge
 * staleness; the target `state` is passed in separately. */
interface PlexPlaylistView {
  updated_at: string;
  plex?: Record<string, PlexTargetState>;
}

/** Visual tone for a sync status, mapped to a leading icon + a semantic text
 * color so the state reads at a glance (the text label stays the non-color
 * carrier for screen readers / color-blind users). */
export type StatusTone = "success" | "warning" | "destructive" | "muted";

export interface SyncStatus {
  label: string;
  tone: StatusTone;
}

/** Which wire a `partial` state is read on, because the two carry different
 * detail and therefore support different (honest) wording.
 *
 * "detail" is the single-playlist response: it CARRIES `missing_tracks`, so
 * each miss's reason is known and the two kinds (a track Plex hasn't got vs. a
 * duplicate the Plex copy holds once) can be named apart.
 *
 * "summary" is the list wire: `missing_tracks` is deliberately EMPTY there, so
 * the absent-vs-duplicate split is unknowable — and opening the playlist
 * reveals the detail without any re-sync. Neither the "re-sync to see which"
 * hint nor the split is true of that wire, so only the wording that is true of
 * both kinds ("not on the Plex copy") is used. */
export type SyncStatusMode = "detail" | "summary";

/** True when ISO instant `a` is strictly later than `b`. Compares parsed
 * instants (not raw strings) so it's robust to timezone-offset / sub-second
 * precision drift between the two timestamps. */
function isAfter(a: string, b: string): boolean {
  return new Date(a).getTime() > new Date(b).getTime();
}

/** How many of the carried miss identities are collapsed duplicates. They are
 * misses — a row of this playlist has no counterpart row on the Plex copy — but
 * they are not absences, so they are counted apart from the "not in Plex"
 * total rather than folded into it. */
function collapsedCount(misses: PlexTargetState["missing_tracks"]): number {
  return misses.filter((miss) => miss.reason === "duplicate_collapsed").length;
}

/** The `partial` label when the carried identities account for every miss, so
 * the split between the two kinds is exact.
 *
 * They are named apart because they are different news: "not in Plex" is a
 * track the Plex library hasn't got, while a collapsed duplicate is a track it
 * HAS — a sync whose only misses are repeats is not "2 not in Plex", which
 * would be the same falsehood the row badge refuses to tell, one level up.
 *
 * A clause is dropped when its count is zero; a state with neither keeps the
 * bare count wording it has always had. */
function partialLabel(absent: number, collapsed: number): string {
  const clauses: string[] = [];
  if (absent > 0 || collapsed === 0) {
    clauses.push(`${absent} not in Plex`);
  }
  if (collapsed > 0) {
    clauses.push(
      `${collapsed} duplicate ${collapsed === 1 ? "row" : "rows"}; Plex keeps one of each`,
    );
  }
  return clauses.join("; ");
}

/** The one-line Plex sync status for one target (admin or a fan-out user).
 * "Out of date" wins when the playlist changed after this target's last push.
 * An absent/unknown state falls back to `notSyncedLabel` (e.g. a freshly-checked
 * target that hasn't synced yet).
 *
 * `mode` selects the wire the state came off (see `SyncStatusMode`): the
 * detail wire keeps the full, per-kind wording; the summary wire says only the
 * honest "not on the Plex copy", never "re-sync to see which" and never a split
 * it cannot know. The detail wording — and thus the detail page's rendered
 * output — is unchanged when `mode` is left at its "detail" default. */
export function syncStatus(
  state: PlexTargetState | undefined,
  playlist: PlexPlaylistView,
  notSyncedLabel: string,
  mode: SyncStatusMode = "detail",
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
      if (mode === "summary") {
        // List wire: `missing_tracks` is never carried here, so neither the
        // absent/duplicate split nor a re-sync remedy is true — opening the
        // playlist shows the detail. Say only what holds for both kinds.
        return { label: `${state.missing} not on the Plex copy`, tone: "warning" };
      }
      const marked = state.missing_tracks.length;
      const collapsed = collapsedCount(state.missing_tracks);
      if (marked >= state.missing) {
        // Every miss is carried, so every reason is known: name the two kinds.
        return {
          label: partialLabel(Math.max(state.missing - collapsed, 0), collapsed),
          tone: "warning",
        };
      }
      // Fewer carried identities than misses — the server caps the list
      // (MISSING_TRACKS_CAP). Only those rows can wear a badge, so say which
      // ones the badges cover; otherwise the unmarked remainder reads as fine.
      // The unmarked remainder's REASONS are unknown as well, so the total
      // can't be split here and mustn't be called absent: it takes the wording
      // that is true of both kinds instead ("not on the Plex copy" covers a
      // track Plex hasn't got AND a repeat of one it holds once). Guessing
      // from the visible slice would be worse than vague — the server lists
      // collapsed duplicates after the absences, so truncation hides them
      // first and the slice reads as all-absent precisely when it isn't.
      // `marked === 0` can't come from the cap (it truncates to 200, never 0):
      // it means the state predates the identities, which one re-sync fixes —
      // and a record that predates them predates collapsed duplicates too, so
      // that arm alone can still say plainly that they aren't in Plex.
      return {
        label:
          marked === 0
            ? `${state.missing} not in Plex; re-sync to see which`
            : `${state.missing} not on the Plex copy; first ${marked} marked`,
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
export function adminSyncStatus(playlist: PlexPlaylistView): SyncStatus {
  return syncStatus(playlist.plex?.admin, playlist, "Not synced to Plex");
}

/**
 * Render a sync status with a leading concept icon + semantic color. The text
 * label is always present (the color/icon are emphasis, not the only signal).
 *
 * Presentational: the `StatusLine` component lives here (alongside the status
 * computation it renders) so the playlists list and the playlist detail share
 * the same badge markup. `label` stays the non-color carrier of meaning — it is
 * what gets queried on the list rows; the icon and tone are only emphasis.
 */
export function StatusLine({ status }: { status: SyncStatus }) {
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

/** Severity order for aggregating several targets into one row badge. A bad
 * target always outranks a good one, so a failed/out-of-date fan-out target
 * can never be hidden behind the admin's healthy copy. */
const TONE_RANK: Record<StatusTone, number> = {
  muted: 0,
  success: 1,
  warning: 2,
  destructive: 3,
};

/** The one whole-playlist badge the list row shows, aggregated across every
 * Plex target. Reads each target on the "summary" wire, since this is the list.
 *
 * Returns `undefined` when the playlist has NO Plex state at all (no targets),
 * so a never-synced playlist stays a quiet row — a muted "Not synced" on every
 * quiet row would be noise, and a bad target is what actually needs surfacing.
 *
 * The rule is severity-first: the worst target always wins the badge
 * (destructive > warning > success > muted). A single target — the common
 * admin-only case — reads as exactly that target's status: one clean badge.
 * A lone problem among otherwise-healthy targets is named by its own label
 * (its error, "Out of date; re-sync", …) rather than buried. Only a row with
 * SEVERAL problem targets gets a neutral, non-misleading "N targets …" phrase,
 * since their labels (and, for partial, their exact misses) differ per target.
 *
 * Trade-off: for multi-target rows we deliberately do NOT try to stitch
 * target-specific wording (e.g. per-target partial counts) into one line — the
 * detail is reachable one click away and per-target labels would not compose
 * honestly. The invariant we keep is that trouble is never quieter than the
 * admin copy and the badge tone always reflects the worst target. */
export function playlistSyncStatus(
  playlist: PlexPlaylistView,
): SyncStatus | undefined {
  const states = Object.values(playlist.plex ?? {});
  if (states.length === 0) {
    return undefined;
  }

  const per = states.map((state) => syncStatus(state, playlist, "Not synced", "summary"));
  const worst = per.reduce((a, b) => (TONE_RANK[b.tone] > TONE_RANK[a.tone] ? b : a));

  // Only "nothing to sync" / "not synced" — quiet tier; surface the first.
  if (TONE_RANK[worst.tone] === 0) {
    return per[0];
  }
  if (TONE_RANK[worst.tone] === 3) {
    // At least one failed. A single failure shows its real error text; several
    // failures do not share one, so count them instead of guessing a message.
    const failed = per.filter((s) => s.tone === "destructive");
    return failed.length === 1
      ? failed[0]
      : { label: `Sync failed on ${failed.length} targets`, tone: "destructive" };
  }
  if (TONE_RANK[worst.tone] === 2) {
    // No failure; out-of-date / partial / unmatched copies are the problem.
    const warned = per.filter((s) => s.tone === "warning");
    return warned.length === 1
      ? warned[0]
      : { label: `${warned.length} targets need attention`, tone: "warning" };
  }
  // Worst is "Synced" (everything at or below it: a healthy admin copy, maybe
  // a quiet "nothing to send" fan-out) — the good news stands.
  return { label: "Synced", tone: "success" };
}
