import { useQuery } from "@tanstack/react-query";
import {
  CircleCheck,
  CircleSlash,
  CopyCheck,
  FolderInput,
  ListMusic,
  Loader2,
  Search,
  Settings,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  Link,
  Outlet,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router";

import { client } from "@/api/client";
import { ArtistArtBackfillBanner } from "@/components/ArtistArtBackfillBanner";
import { LyricsBackfillBanner } from "@/components/LyricsBackfillBanner";
import { ReorganizeBanner } from "@/components/ReorganizeBanner";
import { ReorganizeNoticeProvider } from "@/components/reorganize/reorganizeNotice";
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

  return (
    // `role="search"` landmark (an explicit role rather than the <search>
    // element, which React/jsdom here don't map to the role).
    <div role="search" className="relative max-w-md min-w-0 flex-1">
      <Search
        className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
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
        className="min-w-0 pl-9"
      />
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
 * Compact backend health indicator in the header. Status is conveyed by THREE
 * carriers, not color alone (WCAG 1.4.1): a shape-distinct icon (check /
 * slashed-circle / spinner), a short visible text label, and the dot color.
 */
export function HealthStatus() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
  });

  const reachable = !isError && !isPending;
  const label = isPending ? "Checking" : reachable ? "Online" : "Offline";
  const description = isPending
    ? "Checking backend"
    : reachable
      ? `Backend online (v${data.version})`
      : "Backend unreachable";

  const Icon = isPending ? Loader2 : reachable ? CircleCheck : CircleSlash;

  return (
    <span
      className="flex items-center gap-1.5 text-sm"
      title={description}
      aria-label={description}
      role="status"
    >
      <Icon
        aria-hidden="true"
        className={cn(
          "size-4",
          isPending
            ? "text-muted-foreground animate-spin"
            : reachable
              ? "text-success"
              : "text-destructive",
        )}
      />
      {/* Collapse to icon-only below sm to save header width; the wrapper's
          title + aria-label still convey the full status. */}
      <span
        className={cn(
          "hidden sm:inline",
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
  );
}

/**
 * App shell: persistent header chrome wrapping the routed page via `<Outlet>`.
 * The browse hierarchy is a single artist spine (roster -> artist -> album), so
 * the header has no section tabs — just the brand (-> roster home) and the
 * health status. Future nav (Search, Playlists, Settings) lands here later.
 */
export function App() {
  const location = useLocation();
  return (
    <ReorganizeNoticeProvider>
      <div className="bg-background text-foreground min-h-svh">
        <header className="border-border bg-background/80 sticky top-0 z-10 border-b backdrop-blur">
          <div className="mx-auto flex max-w-7xl items-center gap-4 px-6 py-4">
            <h1 className="shrink-0 text-xl font-semibold tracking-tight">
              <Link
                to="/"
                className="focus-visible:ring-ring rounded-sm focus-visible:ring-2 focus-visible:outline-none"
              >
                MusicDrop
              </Link>
            </h1>
            <HeaderSearch />
            <nav
              className="ml-auto flex shrink-0 items-center gap-4"
              aria-label="Primary"
            >
              <Link
                to="/import"
                aria-label="Import"
                aria-current={
                  location.pathname.startsWith("/import") ? "page" : undefined
                }
                className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex items-center gap-1.5 rounded-sm text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
              >
                <FolderInput className="size-4" aria-hidden="true" />
                {/* Label hides below sm to preserve header width, like the health
                  status; the Link's aria-label carries the name when icon-only. */}
                <span className="hidden sm:inline">Import</span>
              </Link>
              <Link
                to="/settings"
                aria-label="Settings"
                aria-current={
                  location.pathname.startsWith("/settings") ? "page" : undefined
                }
                className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex items-center gap-1.5 rounded-sm text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
              >
                <Settings className="size-4" aria-hidden="true" />
                <span className="hidden sm:inline">Settings</span>
              </Link>
              <Link
                to="/playlists"
                aria-label="Playlists"
                aria-current={
                  location.pathname.startsWith("/playlists") ? "page" : undefined
                }
                className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex items-center gap-1.5 rounded-sm text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
              >
                <ListMusic className="size-4" aria-hidden="true" />
                <span className="hidden sm:inline">Playlists</span>
              </Link>
              <Link
                to="/duplicates"
                aria-label="Duplicates"
                aria-current={
                  location.pathname.startsWith("/duplicates")
                    ? "page"
                    : undefined
                }
                className="text-muted-foreground hover:text-foreground focus-visible:ring-ring flex items-center gap-1.5 rounded-sm text-sm font-medium focus-visible:ring-2 focus-visible:outline-none"
              >
                <CopyCheck className="size-4" aria-hidden="true" />
                <span className="hidden sm:inline">Duplicates</span>
              </Link>
            </nav>
            <div className="shrink-0">
              <HealthStatus />
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-6 py-8">
          <LyricsBackfillBanner />
          <ArtistArtBackfillBanner />
          <ReorganizeBanner />
          <Outlet />
        </main>
      </div>
    </ReorganizeNoticeProvider>
  );
}
