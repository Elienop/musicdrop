import { useState } from "react";

/** Where this browser keeps Add from folder's Recent list. */
export const RECENT_FOLDERS_KEY = "musicdrop.recentFolders";
/** How many Recent folders are kept, as Sonarr keeps (decision #54). */
export const RECENT_FOLDERS_MAX = 10;

/** One Recent folder: the path the server accepted, and when (epoch ms). */
export interface RecentFolder {
  path: string;
  at: number;
}

function isRecentFolder(value: unknown): value is RecentFolder {
  if (typeof value !== "object" || value === null) return false;
  const { path, at } = value as Record<string, unknown>;
  return (
    typeof path === "string" &&
    path !== "" &&
    typeof at === "number" &&
    Number.isFinite(at)
  );
}

/** The stored list, newest first, or `null` when storage can't be read at
 * all (blocked). Foreign and repeated entries are skipped, and the next save
 * writes the list without them. */
function readStored(): RecentFolder[] | null {
  let raw: string | null;
  try {
    raw = localStorage.getItem(RECENT_FOLDERS_KEY);
  } catch {
    return null; // storage blocked — no Recent section
  }
  if (raw === null) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const list: RecentFolder[] = [];
  for (const row of parsed) {
    // Full: stop, however long a foreign writer made the stored list.
    if (list.length === RECENT_FOLDERS_MAX) break;
    if (!isRecentFolder(row) || list.some((kept) => kept.path === row.path)) {
      continue;
    }
    list.push({ path: row.path, at: row.at });
  }
  return list;
}

/** Save `list`; false when storage refuses (blocked, or full). */
function writeStored(list: RecentFolder[]): boolean {
  try {
    localStorage.setItem(RECENT_FOLDERS_KEY, JSON.stringify(list));
    return true;
  } catch {
    return false;
  }
}

/**
 * Add from folder's Recent folders, kept in this browser only. Every storage
 * access is guarded (the pattern of `usePageSize`): blocked storage reads as
 * an empty list and the page shows no Recent section.
 *
 * `add` puts a path at the top (the same text moves up, never twice) and keeps
 * the newest {@link RECENT_FOLDERS_MAX}. The caller adds only a path the server
 * accepted. `remove` drops one.
 */
export function useRecentFolders(): {
  recent: readonly RecentFolder[];
  add: (path: string) => void;
  remove: (path: string) => void;
} {
  const [recent, setRecent] = useState<RecentFolder[]>(
    () => readStored() ?? [],
  );

  // Both read storage afresh, so another tab's add is kept, not overwritten.
  function save(next: (list: RecentFolder[]) => RecentFolder[]) {
    const stored = readStored();
    if (stored === null) return;
    const list = next(stored);
    if (writeStored(list)) setRecent(list);
  }

  return {
    recent,
    add: (path) =>
      save((list) =>
        [
          { path, at: Date.now() },
          ...list.filter((row) => row.path !== path),
        ].slice(0, RECENT_FOLDERS_MAX),
      ),
    remove: (path) => save((list) => list.filter((row) => row.path !== path)),
  };
}

/** Calendar days from `at` to `now`, in this browser's time zone. */
function daysAgo(at: number, now: Date): number {
  const day = (d: Date) =>
    Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86_400_000;
  return day(now) - day(new Date(at));
}

const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

/** When a Recent folder was used: `Today`, `Yesterday`, `3 days ago`, and
 * from a week back, the date. */
export function recentDayLabel(at: number, now: Date = new Date()): string {
  const days = daysAgo(at, now);
  if (days >= 0 && days < 7) {
    const words = RELATIVE.format(-days, "day");
    return words.charAt(0).toUpperCase() + words.slice(1);
  }
  return new Date(at).toLocaleDateString(undefined, { dateStyle: "medium" });
}
