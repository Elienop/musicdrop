import { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router";

/** Exact-pathname titles for the static routes (see main.tsx's route table). */
const EXACT_TITLES = new Map<string, string>([
  ["/", "Overview"],
  ["/artists", "Artists"],
  ["/search", "Search"],
  ["/browse", "Browse"],
  ["/review", "Review"],
  ["/import", "Add from folder"],
  ["/settings", "Settings"],
  ["/duplicates", "Duplicates"],
  ["/playlists", "Playlists"],
]);

/** Prefix fallbacks for dynamic segments — most specific first. Opaque ids
 * (album/playlist/import index) keep the static section title; pages with the
 * data loaded can refine `document.title` later. */
const PREFIX_TITLES: readonly (readonly [string, string])[] = [
  ["/review/bank/", "Review decision"],
  ["/import/albums/", "Import decision"],
  ["/import/", "Add from folder"],
  ["/albums/", "Album"],
  ["/settings/", "Settings"],
  ["/playlists/", "Playlists"],
];

/** Resolve the human page title for a pathname; unknown routes (the `*`
 * NotFound catch-all) read "Not found". Exported for unit tests. */
export function titleForPathname(pathname: string): string {
  const exact = EXACT_TITLES.get(pathname);
  if (exact !== undefined) {
    return exact;
  }
  if (pathname.startsWith("/artists/")) {
    // /artists/:artistName — the segment IS the artist, so the title can
    // carry it ("Daft Punk — MusicDrop"). Malformed escapes fall back to the
    // section title instead of throwing.
    try {
      return decodeURIComponent(pathname.slice("/artists/".length));
    } catch {
      return "Artists";
    }
  }
  for (const [prefix, title] of PREFIX_TITLES) {
    if (pathname.startsWith(prefix)) {
      return title;
    }
  }
  return "Not found";
}

/** True when the element is a text-entry target the user may be typing in —
 * moving focus to the h1 mid-type (e.g. the header search navigating to
 * /search on its debounce) would yank the caret away. Click-driven navigation
 * (focus on a link/button) keeps the a11y focus move. */
export function isTypingTarget(el: Element | null): boolean {
  if (!(el instanceof HTMLElement)) {
    return false;
  }
  return (
    el.tagName === "INPUT" ||
    el.tagName === "TEXTAREA" ||
    el.isContentEditable === true
  );
}

/**
 * Makes route changes non-silent: on PATHNAME change (search-param churn on
 * Browse/Search never reaches the effect) it sets
 * `document.title = "<Page> — MusicDrop"`, mirrors the page name into an
 * always-mounted polite live region, and moves focus to the page h1 — the
 * `tabIndex={-1}` heading PageHeader renders. The focus move is skipped on
 * the FIRST render so initial page load keeps the browser's default focus.
 * Built in Phase 1; mounted into the shell in Phase 2.
 */
export function RouteAnnouncer() {
  const { pathname } = useLocation();
  const [announcement, setAnnouncement] = useState("");
  // null until the first effect has run — the first-render sentinel.
  const previousPathname = useRef<string | null>(null);

  useEffect(() => {
    const title = titleForPathname(pathname);
    document.title = `${title} - MusicDrop`;
    setAnnouncement(title);
    if (
      previousPathname.current !== null &&
      previousPathname.current !== pathname &&
      !isTypingTarget(document.activeElement)
    ) {
      document.querySelector<HTMLElement>('h1[tabindex="-1"]')?.focus();
    }
    previousPathname.current = pathname;
  }, [pathname]);

  return (
    <p className="sr-only" role="status">
      {announcement}
    </p>
  );
}
