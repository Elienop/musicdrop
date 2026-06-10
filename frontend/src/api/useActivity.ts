// frontend/src/api/useActivity.ts
//
// The activity read model (spec §2): ONE composition hook over the five
// existing polling hooks — no new polling, no backend change. Cadences stay
// owned by the source hooks (import/acquisition 5s↔30s adaptive; lyrics,
// artist-art and reorganize poll at 1s only while running). Terminal rows
// exist only while the underlying hook still reports a terminal phase — no
// synthetic history. Dismissed failed-row ids live in sessionStorage
// ("md.activity.dismissed") behind a module-level useSyncExternalStore store,
// so the popover's dismiss() and App's useActivity() stay in lockstep without
// a context provider.
import { useSyncExternalStore } from "react";

import { useAcquisitionStatus } from "@/api/useAcquisitionStatus";
import { useActiveImport } from "@/api/useActiveImport";
import { useArtistArtBackfillStatus } from "@/api/useArtistArt";
import { useLyricsBackfillStatus } from "@/api/useLyricsBackfill";
import { useReorganizeStatus } from "@/api/useReorganize";
import type { AcquisitionQueueStatus } from "@/api/useAcquisitionStatus";
import type { ActiveImportStatus } from "@/api/useActiveImport";
import type { ArtistArtBackfillStatus } from "@/api/useArtistArt";
import type { LyricsBackfillStatus } from "@/api/useLyricsBackfill";
import type { ReorganizeBackfillStatus } from "@/api/useReorganize";

export type ActivityRow = {
  id: string;
  kind: "import" | "lyrics" | "artist-art" | "reorganize" | "acquisition";
  label: string;
  scope?: string;
  state: "running" | "failed" | "done";
  progress?: { done: number; total: number };
  countsText?: string;
  href?: string;
};

// ---------------------------------------------------------------------------
// Dismissals — sessionStorage-backed external store.

const DISMISSED_KEY = "md.activity.dismissed";

const listeners = new Set<() => void>();

// Snapshot cache keyed on the raw storage string: useSyncExternalStore needs
// a STABLE reference between writes (a fresh Set per getSnapshot call would
// re-render forever). `undefined` sentinel ≠ null so the first read parses.
let lastRaw: string | null | undefined;
let lastSet: Set<string> = new Set();

function readDismissed(): Set<string> {
  const raw = sessionStorage.getItem(DISMISSED_KEY);
  if (raw === lastRaw) {
    return lastSet;
  }
  lastRaw = raw;
  try {
    const parsed: unknown = raw === null ? [] : JSON.parse(raw);
    lastSet = new Set(
      Array.isArray(parsed)
        ? parsed.filter((entry): entry is string => typeof entry === "string")
        : [],
    );
  } catch {
    // Corrupt storage (hand-edited devtools, etc.) reads as "nothing
    // dismissed" — failed rows reappear, which is the safe direction.
    lastSet = new Set();
  }
  return lastSet;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function dismiss(id: string): void {
  const next = new Set(readDismissed());
  next.add(id);
  sessionStorage.setItem(DISMISSED_KEY, JSON.stringify([...next]));
  for (const listener of listeners) {
    listener();
  }
}

/** Dismissed failed-row ids (session-scoped). All call sites share one
 * store, so a dismiss in the popover immediately drops the row from every
 * `useActivity()` consumer. */
export function useActivityDismissals(): {
  dismissed: Set<string>;
  dismiss: (id: string) => void;
} {
  const dismissed = useSyncExternalStore(subscribe, readDismissed);
  return { dismissed, dismiss };
}

// ---------------------------------------------------------------------------
// Per-source row builders. Each returns null when the source has nothing to
// show ("idle"/"stopped" phases — a user-initiated stop is not an outcome,
// matching the old banners' posture).

/** "5 found · 2 skipped" — zero segments dropped; all-zero → undefined. */
function outcomeCounts(
  parts: ReadonlyArray<readonly [number, string]>,
): string | undefined {
  const text = parts
    .filter(([n]) => n > 0)
    .map(([n, word]) => `${n} ${word}`)
    .join(" · ");
  return text === "" ? undefined : text;
}

function importRow(status: ActiveImportStatus | undefined): ActivityRow | null {
  if (status === undefined || !status.active) {
    return null;
  }
  // An inbox-origin import is already represented by the acquisition row
  // ("Importing from inbox") — rendering both would double-count one job.
  if (status.origin === "inbox") {
    return null;
  }
  // NOTE: the probe has NO terminal phase (active just flips false), so the
  // import row is always "running" and simply disappears when the job ends.
  return {
    id: `import:${status.job_id ?? "active"}`,
    kind: "import",
    label: "Import",
    state: "running",
    countsText:
      status.needs_review_count > 0
        ? `${status.needs_review_count} awaiting review`
        : undefined,
    href:
      status.job_id != null ? `/import?job=${status.job_id}` : "/import",
  };
}

function acquisitionRow(
  status: AcquisitionQueueStatus | undefined,
): ActivityRow | null {
  // The phase enum is only "idle" | "running" — the durable set-aside/failed
  // totals are the Review page's surface, not activity rows.
  if (status === undefined || status.phase !== "running") {
    return null;
  }
  return {
    id: "acquisition:queue",
    kind: "acquisition",
    label: "Importing from inbox",
    scope: status.current ?? undefined,
    state: "running",
    countsText: status.queued > 0 ? `${status.queued} queued` : undefined,
    href: "/review",
  };
}

function lyricsRow(
  status: LyricsBackfillStatus | undefined,
): ActivityRow | null {
  if (status === undefined) {
    return null;
  }
  const id = `lyrics:${status.job_id ?? "job"}`;
  const label = status.album_id != null ? "Fetching lyrics" : "Lyrics backfill";
  const href =
    status.album_id != null ? `/albums/${status.album_id}` : "/settings";
  if (status.phase === "running") {
    return {
      id, kind: "lyrics", label, scope: status.scope_label, state: "running",
      progress: { done: status.processed, total: status.total },
      href,
    };
  }
  if (status.phase === "failed") {
    return {
      id, kind: "lyrics", label, scope: status.scope_label, state: "failed",
      countsText: status.error ?? undefined,
      href,
    };
  }
  if (status.phase === "done") {
    return {
      id, kind: "lyrics", label, scope: status.scope_label, state: "done",
      countsText: outcomeCounts([
        [status.found, "found"],
        [status.not_found, "not found"],
        [status.skipped, "skipped"],
        [status.failed, "failed"],
      ]),
      href,
    };
  }
  return null;
}

function artistArtRow(
  status: ArtistArtBackfillStatus | undefined,
): ActivityRow | null {
  if (status === undefined) {
    return null;
  }
  const id = `artist-art:${status.job_id ?? "job"}`;
  if (status.phase === "running") {
    return {
      id, kind: "artist-art", label: "Writing artist art",
      scope: status.scope_label, state: "running",
      progress: { done: status.processed, total: status.total },
      href: "/settings",
    };
  }
  if (status.phase === "failed") {
    return {
      id, kind: "artist-art", label: "Writing artist art",
      scope: status.scope_label, state: "failed",
      countsText: status.error ?? undefined,
      href: "/settings",
    };
  }
  if (status.phase === "done") {
    return {
      id, kind: "artist-art", label: "Writing artist art",
      scope: status.scope_label, state: "done",
      countsText: outcomeCounts([
        [status.written, "written"],
        [status.skipped, "skipped"],
        [status.failed, "failed"],
      ]),
      href: "/settings",
    };
  }
  return null;
}

function reorganizeRow(
  status: ReorganizeBackfillStatus | undefined,
): ActivityRow | null {
  if (status === undefined) {
    return null;
  }
  const id = `reorganize:${status.job_id ?? "job"}`;
  if (status.phase === "running") {
    return {
      id, kind: "reorganize", label: "Reorganize",
      scope: status.scope_label, state: "running",
      progress: { done: status.processed, total: status.total },
      href: "/settings",
    };
  }
  if (status.phase === "failed") {
    return {
      id, kind: "reorganize", label: "Reorganize",
      scope: status.scope_label, state: "failed",
      countsText: status.error ?? undefined,
      href: "/settings",
    };
  }
  if (status.phase === "done") {
    return {
      id, kind: "reorganize", label: "Reorganize",
      scope: status.scope_label, state: "done",
      countsText: outcomeCounts([
        [status.moved, "moved"],
        [status.skipped, "skipped"],
        [status.failed, "failed"],
      ]),
      href: "/settings",
    };
  }
  return null;
}

// ---------------------------------------------------------------------------

/** Compose the five job sources into the activity rows the topbar popover
 * renders and the toast effect diffs. Dismissed FAILED rows are excluded
 * (running/done rows never are — ids are job-scoped, so a new run is a new
 * id and a stale dismissal cannot hide it). */
export function useActivity(): { rows: ActivityRow[]; runningCount: number } {
  const importStatus = useActiveImport().data;
  const acquisitionStatus = useAcquisitionStatus().data;
  const lyricsStatus = useLyricsBackfillStatus().data;
  const artistArtStatus = useArtistArtBackfillStatus().data;
  const reorganizeStatus = useReorganizeStatus().data;
  const { dismissed } = useActivityDismissals();

  const rows = [
    importRow(importStatus),
    acquisitionRow(acquisitionStatus),
    lyricsRow(lyricsStatus),
    artistArtRow(artistArtStatus),
    reorganizeRow(reorganizeStatus),
  ]
    .filter((row): row is ActivityRow => row !== null)
    .filter((row) => row.state !== "failed" || !dismissed.has(row.id));

  return {
    rows,
    runningCount: rows.filter((row) => row.state === "running").length,
  };
}
