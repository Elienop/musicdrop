import { useEffect, useRef } from "react";
import { useLocation } from "react-router";

/**
 * Cold-load focus repair for detail routes (Phase 4; deferred from the
 * Phase 2/3 reviews): on a FIRST visit a detail route renders its skeleton,
 * which has no h1, so RouteAnnouncer's `h1[tabindex="-1"]` focus is a no-op
 * and keyboard focus sits on <body>. Pages call this with `ready=true` once
 * their data has rendered; on that commit, IF nothing meaningful holds focus
 * (activeElement is still <body>), the page h1 receives focus.
 *
 * - Same selector as RouteAnnouncer — one h1 per route is a Phase 3
 *   invariant, so the document-level query IS the page h1.
 * - One-shot per pathname (useRef sentinel): refetch flickers, StrictMode's
 *   double effect, and error→retry cycles never re-grab focus.
 * - Never steals: if a control holds focus when data lands, the shot is
 *   consumed without moving focus.
 * - Client-side navs are unaffected: RouteAnnouncer still owns the
 *   pathname-change focus; when both fire (cached data, focused link
 *   unmounted) they target the same h1.
 */
export function useDeferredH1Focus(ready: boolean): void {
  const { pathname } = useLocation();
  const done = useRef<string | null>(null);

  useEffect(() => {
    if (!ready || done.current === pathname) {
      return;
    }
    // Consume the shot even when focus is elsewhere: by the time data lands
    // with focus on a control, the user is oriented — never steal later.
    done.current = pathname;
    if (document.activeElement !== document.body) {
      return;
    }
    document.querySelector<HTMLElement>('h1[tabindex="-1"]')?.focus();
  }, [ready, pathname]);
}
