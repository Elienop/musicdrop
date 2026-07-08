import { useSearchParams } from "react-router";

import {
  PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  type PageSize,
} from "@/components/system/Pagination";

const STORAGE_KEY = "musicdrop.pageSize";

/** Parse a raw candidate into a known page size, or null if it's foreign. */
function parseSize(raw: string | null): PageSize | null {
  if (raw === null) return null;
  const n = Number(raw);
  return (PAGE_SIZE_OPTIONS as readonly number[]).includes(n)
    ? (n as PageSize)
    : null;
}

function readStored(): PageSize | null {
  try {
    return parseSize(localStorage.getItem(STORAGE_KEY));
  } catch {
    return null; // storage blocked — behave as "no preference"
  }
}

/**
 * The effective grid page size and its setter, for pages that keep
 * `offset` in the URL (Browse, Artists).
 *
 * Resolution: a valid `?limit=` in the URL → the saved preference
 * (localStorage, shared across pages) → PAGE_SIZE. Foreign values in
 * either place are ignored, never rewritten.
 *
 * `setPageSize` saves the preference, then in ONE URL update sets `limit`
 * and re-anchors `offset` to the page boundary containing the current
 * first item (`floor(offset / n) * n`) — the items on screen stay on
 * screen and Back is a single step.
 */
export function usePageSize(): {
  pageSize: PageSize;
  setPageSize: (n: PageSize) => void;
} {
  const [searchParams, setSearchParams] = useSearchParams();
  const pageSize =
    parseSize(searchParams.get("limit")) ?? readStored() ?? PAGE_SIZE;

  const setPageSize = (n: PageSize) => {
    try {
      localStorage.setItem(STORAGE_KEY, String(n));
    } catch {
      // storage blocked — the URL still carries the choice for this visit
    }
    const next = new URLSearchParams(searchParams);
    next.set("limit", String(n));
    const offset = Math.max(Number(searchParams.get("offset")) || 0, 0);
    const anchored = Math.floor(offset / n) * n;
    if (anchored <= 0) next.delete("offset");
    else next.set("offset", String(anchored));
    setSearchParams(next);
  };

  return { pageSize, setPageSize };
}
