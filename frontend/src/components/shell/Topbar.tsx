// frontend/src/components/shell/Topbar.tsx
//
// The shell topbar (spec §2): hamburger (mobile nav) · the library search
// box · a right-side slot for the activity button + health status.
// HeaderSearch and HealthStatus were moved here from App.tsx with their
// logic verbatim (this file is their canonical home; App.tsx's copies are
// deleted in the Task-8 shell swap — until then AppTopbar is simply not
// mounted, so the app never renders two headers). Additions over the
// originals: a global ⌘K/Ctrl+K shortcut focusing the search box (+ kbd
// hint), and Phosphor concept icons in place of the original glyphs.

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";

import { client } from "@/api/client";
import {
  // Never shadow the global Error — fetchHealth throws it.
  Error as ErrorIcon,
  Online,
  Search,
  Spinner,
} from "@/components/icons";
import { MobileNav } from "@/components/shell/MobileNav";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/** Debounce (ms) before a keystroke is reflected into the URL / fired as a
 * query — long enough to avoid a request per character, short enough to feel
 * live. */
const SEARCH_DEBOUNCE_MS = 250;

/**
 * Global header search box. The query lives in the URL (`/search?q=`) so it's
 * bookmarkable and back works; this input is a debounced editor for it. Typing
 * from any page navigates to `/search`. While the user is on `/search`, URL
 * updates use `replace` so each keystroke doesn't pile onto the history stack.
 * ⌘K / Ctrl+K focuses the box from anywhere.
 */
function HeaderSearch() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const onSearchPage = location.pathname === "/search";
  const urlQuery = onSearchPage ? (searchParams.get("q") ?? "") : "";

  // Local editor state, seeded from the URL — the URL is the OUTPUT of typing.
  const [value, setValue] = useState(urlQuery);
  const inputRef = useRef<HTMLInputElement>(null);
  // Skip the very first debounce tick so merely mounting (e.g. landing on
  // /search?q=foo) doesn't immediately re-navigate.
  const mounted = useRef(false);

  // Re-sync the box FROM the URL on external navigation (Back/forward, a
  // deep-link), but ONLY when the user isn't editing — re-syncing while the box
  // is focused would fight the user mid-type. The no-loop guard below keeps the
  // two directions from ping-ponging.
  useEffect(() => {
    if (document.activeElement !== inputRef.current) {
      setValue(urlQuery);
    }
  }, [urlQuery]);

  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    const id = setTimeout(() => {
      const next = value.trim();
      // Only navigate when the term actually differs from what's in the URL,
      // so syncing the box from the URL doesn't loop.
      if (next === urlQuery.trim()) {
        return;
      }
      navigate(`/search?q=${encodeURIComponent(next)}`, {
        replace: onSearchPage,
      });
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [value, urlQuery, onSearchPage, navigate]);

  // NEW vs the App.tsx original: a global ⌘K / Ctrl+K shortcut focuses the
  // box from anywhere (spec §2). Window-level listener, cleaned up on
  // unmount; preventDefault stops the browser's own ⌘K behaviors.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    // `role="search"` landmark (an explicit role rather than the <search>
    // element, which React/jsdom here don't map to the role).
    <div role="search" className="relative max-w-lg min-w-0 flex-1">
      <Search
        className="text-muted-foreground pointer-events-none absolute top-1/2 left-3.5 size-5 -translate-y-1/2"
        aria-hidden="true"
      />
      <Input
        ref={inputRef}
        type="search"
        aria-label="Search library"
        // Short, fixed placeholder so it isn't crushed/ellipsised at ~360px
        // (an attribute can't be swapped per-breakpoint via CSS); the leading
        // icon + aria-label carry the affordance.
        placeholder="Search…"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        // Scaled up with the topbar (the 56px activity action): taller box,
        // base text, roomier insets.
        className="h-12 min-w-0 rounded-lg pl-11 text-base md:pr-14"
      />
      {/* Decorative shortcut hint; hidden below md where there's rarely a
          hardware keyboard (and where it would eat input width). */}
      <kbd
        aria-hidden="true"
        className="border-border bg-surface-card text-muted-foreground pointer-events-none absolute top-1/2 right-3 hidden -translate-y-1/2 rounded border px-1.5 py-0.5 font-sans text-[10px] md:inline-block"
      >
        ⌘K
      </kbd>
    </div>
  );
}

async function fetchHealth() {
  const { data, error } = await client.GET("/api/health");
  if (error || !data) {
    throw new Error("Health check failed");
  }
  return data;
}

/**
 * Render the backend's reported version for display. The two shapes it
 * actually arrives in are BOTH already display-ready:
 *   - a release image: the Docker build arg is the git tag, so "v0.29.1" —
 *     prefixing again gave the "vv0.29.1" that shipped;
 *   - a dev / CI build: settings.version's default, the literal "dev" —
 *     prefixing that would give the equally wrong "vdev".
 * So only a bare numeric version (never produced today, but the obvious way
 * for the tag scheme to change) gets a "v" added.
 */
function formatVersion(version: string): string {
  return /^\d/.test(version) ? `v${version}` : version;
}

/**
 * Compact backend health indicator. Status is conveyed by THREE carriers,
 * not color alone (WCAG 1.4.1): a shape-distinct icon (check / x-circle /
 * spinner), a short visible text label, and the dot color. The running
 * version trails on the right of the same row (expanded sidebar only).
 */
export function HealthStatus({ compact = false }: { compact?: boolean }) {
  const { data, isPending, isError } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
  });

  const reachable = !isError && !isPending;
  const label = isPending ? "Checking" : reachable ? "Online" : "Offline";
  const version = reachable && data ? formatVersion(data.version) : null;
  // REACHABILITY decides the wording; the version is only decoration on top of
  // it. Keying this off `version` instead made a reachable backend reporting an
  // empty version (`settings.version` is a plain `str`, overridable at runtime
  // via MUSICDROP_VERSION) render a green check labelled "Online" while its
  // accessible name said "Backend unreachable". No version → no empty parens.
  const description = isPending
    ? "Checking backend"
    : !reachable
      ? "Backend unreachable"
      : version
        ? `Backend online (${version})`
        : "Backend online";

  const Icon = isPending ? Spinner : reachable ? Online : ErrorIcon;

  return (
    // `w-full` (expanded only) so the version can sit at the right edge of the
    // sidebar's health row; the collapsed rail stays shrink-to-fit.
    <span
      className={cn("flex items-center gap-3 text-sm", !compact && "w-full")}
    >
      {/* The live region is the STATUS only — see the version note below.
          `shrink-0` fixes which half degrades when the row runs out of width:
          the status (icon + "Online") is the meaning and stays intact, so an
          over-long version truncates alone instead of squeezing the label. */}
      <span
        className="flex shrink-0 items-center gap-3"
        title={description}
        aria-label={description}
        role="status"
      >
        <Icon
          aria-hidden="true"
          className={cn(
            "size-5 shrink-0",
            isPending
              ? "text-muted-foreground animate-spin"
              : reachable
                ? "text-success"
                : "text-destructive",
          )}
        />
        {/* Visually icon-only when `compact` (the collapsed sidebar rail) —
            but sr-only, NOT hidden: live regions announce content changes, not
            aria-label changes, so the role="status" wrapper must keep text for
            an Online→Offline flip to announce. (The `sm:` breakpoint on the
            expanded branch never fires: the only mount is Sidebar's aside,
            which is itself `hidden md:flex`, so this label is never rendered
            below sm — it is inert, not a live small-screen design.) */}
        <span
          className={cn(
            compact ? "sr-only" : "hidden sm:inline",
            isPending
              ? "text-muted-foreground"
              : reachable
                ? "text-success"
                : "text-destructive",
          )}
        >
          {label}
        </span>
      </span>
      {/* Version: reference info, deliberately OUTSIDE the live region — it
          never changes while the app runs, so putting a static string inside
          role="status" only risks it being re-announced on every
          Online→Offline flip. `aria-hidden` because the identical string is
          already in the wrapper's aria-label/title, which is ALSO the only
          carrier in the collapsed rail; without it AT would hear the version
          twice, once as a contextless "v0.29.1". Not rendered when compact:
          the narrow rail has no room for it. */}
      {!compact && version && (
        <span
          aria-hidden="true"
          className="text-muted-foreground ml-auto min-w-0 truncate text-xs"
        >
          {version}
        </span>
      )}
    </span>
  );
}

export interface AppTopbarProps {
  /** Right-side chrome — the Task-8 swap passes `<ActivityButton />` and
   * `<HealthStatus />` here, so this component never imports the activity
   * feature itself. */
  children?: ReactNode;
}

export function AppTopbar({ children }: AppTopbarProps) {
  return (
    <header className="border-border bg-surface-raised/80 sticky top-0 z-10 border-b backdrop-blur">
      <div className="flex items-center gap-3 px-4 py-3 md:px-6">
        <MobileNav />
        <HeaderSearch />
        <div className="ml-auto flex shrink-0 items-center gap-3">
          {children}
        </div>
      </div>
    </header>
  );
}
