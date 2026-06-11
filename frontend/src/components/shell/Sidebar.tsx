// frontend/src/components/shell/Sidebar.tsx
//
// The app sidebar (spec §1/§2): grouped Library / Acquire / Manage nav.
// Active state is SECTION-membership driven, not raw path-prefix — routes
// reachable from several places (/albums/*, /search) light the section
// recorded in their router-state origin, defaulting to Library. The item
// pill (violet bg + fill-weight icon + aria-current) only lights when the
// pathname matches the item's own route.
//
// Self-contained: useLocation for active state, localStorage
// ("md.sidebar.collapsed") for the 64px icon-rail collapse. Mounted by App
// in the Task-8 shell swap; hidden below `md`, where MobileNav takes over.

import { useState } from "react";
import { Link, useLocation } from "react-router";

import { useActiveImport } from "@/api/useActiveImport";
import {
  AddFromFolder,
  Artists,
  Back,
  Browse,
  Duplicates,
  Forward,
  Overview,
  Playlists,
  Review,
  Settings,
  type AppIcon,
} from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { LogoMark, LogoWordmark } from "@/components/shell/Logo";
import { HealthStatus } from "@/components/shell/Topbar";
import { cn } from "@/lib/utils";

/** Concept names a nav item can carry — keys into NAV_ICONS (one concept =
 * one icon, mirroring @/components/icons). Extended with "FindMusic" and
 * "Downloads" when the slskd project renders those slots. */
export type NavConcept =
  | "Overview"
  | "Artists"
  | "Browse"
  | "Review"
  | "AddFromFolder"
  | "Playlists"
  | "Duplicates"
  | "Settings";

export interface NavItem {
  concept: NavConcept;
  label: string;
  to: string;
}

export interface NavSection {
  label: string;
  items: NavItem[];
}

/**
 * The sidebar data model (spec §1), shared with MobileNav. Find music
 * (`/find`) and Downloads (`/downloads`) are designed Acquire slots the
 * slskd project will add ABOVE Review — they are NOT rendered until their
 * routes exist (no dead links):
 *
 *   { concept: "FindMusic", label: "Find music", to: "/find" }
 *   { concept: "Downloads", label: "Downloads", to: "/downloads" }
 */
export const NAV_SECTIONS: NavSection[] = [
  {
    label: "Library",
    items: [
      { concept: "Overview", label: "Overview", to: "/" },
      { concept: "Artists", label: "Artists", to: "/artists" },
      { concept: "Browse", label: "Browse", to: "/browse" },
    ],
  },
  {
    label: "Acquire",
    items: [
      { concept: "Review", label: "Review", to: "/review" },
      { concept: "AddFromFolder", label: "Add from folder", to: "/import" },
    ],
  },
  {
    label: "Manage",
    items: [
      { concept: "Playlists", label: "Playlists", to: "/playlists" },
      { concept: "Duplicates", label: "Duplicates", to: "/duplicates" },
      { concept: "Settings", label: "Settings", to: "/settings" },
    ],
  },
];

/** Concept → Phosphor icon, exported so MobileNav renders the exact same
 * glyph for the same concept. */
export const NAV_ICONS: Record<NavConcept, AppIcon> = {
  Overview,
  Artists,
  Browse,
  Review,
  AddFromFolder,
  Playlists,
  Duplicates,
  Settings,
};

/** Exact match for "/", segment-prefix match for everything else (so
 * "/artists/Adele" lights Artists but "/artistsy" would not). Exported so
 * MobileNav's drawer items resolve active state identically. */
export function itemIsActive(pathname: string, to: string): boolean {
  if (to === "/") {
    return pathname === "/";
  }
  return pathname === to || pathname.startsWith(`${to}/`);
}

/** Duck-type the AlbumOrigin router-state contract ({from: {label, to}})
 * without importing the page module that defines it. */
function originTo(state: unknown): string | undefined {
  if (typeof state !== "object" || state === null) {
    return undefined;
  }
  const from = (state as { from?: unknown }).from;
  if (typeof from !== "object" || from === null) {
    return undefined;
  }
  const to = (from as { to?: unknown }).to;
  return typeof to === "string" ? to : undefined;
}

function sectionForRoute(pathname: string): string | undefined {
  for (const section of NAV_SECTIONS) {
    if (section.items.some((item) => itemIsActive(pathname, item.to))) {
      return section.label;
    }
  }
  return undefined;
}

/**
 * Active-SECTION resolution (spec §1: "section membership, not path-prefix").
 * Routes owned by a nav item resolve directly; shared routes (/albums/*,
 * /search) resolve via the `{from: {to}}` origin in router state when its
 * prefix maps to a section, else default to Library.
 */
export function sectionForPathname(pathname: string, state: unknown): string {
  const direct = sectionForRoute(pathname);
  if (direct !== undefined) {
    return direct;
  }
  const origin = originTo(state);
  if (origin !== undefined) {
    const viaOrigin = sectionForRoute(origin);
    if (viaOrigin !== undefined) {
      return viaOrigin;
    }
  }
  return "Library";
}

const COLLAPSE_KEY = "md.sidebar.collapsed";

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSE_KEY) === "1";
  } catch {
    return false; // storage blocked (private mode) — default expanded
  }
}

export interface AppSidebarProps {
  /** Per-concept count badge OVERRIDES (tests use these). When a concept is
   * absent here, the sidebar supplies its own live default — Review reads
   * `useActiveImport().data?.needs_review_count ?? 0` (decisions pending
   * ONLY — the inbox backlog is deliberately NOT summed in; spec §1 badge
   * discipline). Zero (explicit or live) renders no badge; a positive count
   * is folded into the link's aria-label ("Review (3)") either way. */
  badges?: Partial<Record<NavConcept, number>>;
}

export function AppSidebar({ badges }: AppSidebarProps) {
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const activeSection = sectionForPathname(location.pathname, location.state);
  // Self-contained badge source (chosen contract: Sidebar-consumes-hook-
  // itself; App renders <AppSidebar /> bare in the Task-9 swap). The probe
  // never throws; with no handler in a unit test it settles to 0.
  const liveReviewCount = useActiveImport().data?.needs_review_count ?? 0;

  const toggle = () => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
      } catch {
        // Storage blocked — the collapse simply won't persist.
      }
      return next;
    });
  };

  return (
    <aside
      data-collapsed={collapsed}
      className={cn(
        "border-border bg-surface-raised sticky top-0 hidden h-svh shrink-0 flex-col border-r md:flex",
        collapsed ? "w-16" : "w-[230px]",
      )}
    >
      {/* Brand: a LINK to Overview, not a heading (spec §1). */}
      <Link
        to="/"
        aria-label="MusicDrop"
        className={cn(
          "focus-ring mx-3 mt-4 flex h-11 shrink-0 items-center gap-2.5 rounded-md text-base font-semibold tracking-tight",
          collapsed ? "justify-center px-0" : "px-3",
        )}
      >
        {collapsed ? (
          <LogoMark className="h-5 w-auto shrink-0" />
          ) : (
          <LogoWordmark className="h-6 w-auto shrink-0" />
          )}
      </Link>
      <nav aria-label="Primary" className="flex-1 overflow-y-auto px-3 pb-4">
        {NAV_SECTIONS.map((section, index) => {
          const sectionActive = section.label === activeSection;
          return (
            <div
              key={section.label}
              data-section={section.label}
              data-active={sectionActive ? "true" : undefined}
            >
              {/* Rail mode keeps the group SEPARATOR while the label goes
                  sr-only (spec §2). */}
              {collapsed && index > 0 && (
                <div
                  className="border-border mx-2 mt-3 border-t"
                  aria-hidden="true"
                />
              )}
              <div
                className={cn(
                  "px-3 pt-5 pb-1.5 text-[11px] font-semibold tracking-wider uppercase",
                  sectionActive
                    ? "text-foreground/70"
                    : "text-muted-foreground/70",
                  collapsed && "sr-only",
                )}
              >
                {section.label}
              </div>
              <ul className="flex flex-col gap-0.5">
                {section.items.map((item) => {
                  const Icon = NAV_ICONS[item.concept];
                  const active = itemIsActive(location.pathname, item.to);
                  const badge =
                    badges?.[item.concept] ??
                    (item.concept === "Review" ? liveReviewCount : 0);
                  return (
                    <li key={item.to}>
                      <Link
                        to={item.to}
                        aria-label={
                          badge > 0 ? `${item.label} (${badge})` : item.label
                        }
                        aria-current={active ? "page" : undefined}
                        title={collapsed ? item.label : undefined}
                        className={cn(
                          // h-11 = the ≥44px hit area, kept in the rail too.
                          "focus-ring flex h-11 items-center gap-3 rounded-md px-3 text-sm font-normal",
                          active
                            ? "text-primary-light"
                            : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
                          collapsed && "justify-center px-0",
                        )}
                      >
                        <Icon
                          className="size-5 shrink-0"
                          aria-hidden="true"
                        />
                        {!collapsed && (
                          <span className="truncate">{item.label}</span>
                        )}
                        {!collapsed && badge > 0 && (
                          <Badge className="ml-auto px-1.5 py-0 text-xs">
                            {badge}
                          </Badge>
                        )}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </nav>
      {/* Backend health sits at the bottom of the nav column, ABOVE the
          footer separator (moved out of the topbar) — always visible but
          out of the action area. Icon-only in the collapsed rail;
          title/aria carry the full status. */}
      <div
        className={cn(
          "flex h-9 shrink-0 items-center pb-2",
          collapsed ? "justify-center" : "px-6",
        )}
      >
        <HealthStatus compact={collapsed} />
      </div>
      <div className="border-border border-t p-3">
        <button
          type="button"
          onClick={toggle}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!collapsed}
          className={cn(
            "focus-ring text-muted-foreground hover:bg-surface-hover hover:text-foreground flex h-11 items-center gap-3 rounded-md text-sm font-normal",
            collapsed ? "w-full justify-center px-0" : "w-full px-3",
          )}
        >
          {collapsed ? (
            <Forward className="size-5" aria-hidden="true" />
          ) : (
            <Back className="size-5" aria-hidden="true" />
          )}
          {!collapsed && <span>Collapse</span>}
        </button>
      </div>
    </aside>
  );
}
