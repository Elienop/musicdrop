import { useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";

import {
  type BrowseFilters,
  type BrowseSort,
  useBrowseAlbums,
  useBrowseFacets,
} from "@/api/useBrowse";
import {
  AlbumCard,
  AlbumsGridSkeleton,
  GRID_CLASS,
} from "@/components/albums/album-grid";
import {
  Albums,
  Browse,
  Close,
  Expand,
  MusicFallback,
} from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import {
  PAGE_SIZE_OPTIONS,
  PageSizeSelect,
  Pagination,
} from "@/components/system/Pagination";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Skeleton } from "@/components/ui/skeleton";
import { usePageSize } from "@/lib/usePageSize";
import { useRailMaxHeight } from "@/lib/useRailMaxHeight";
import { cn } from "@/lib/utils";

const FACET_FIELDS = [
  { param: "genre", facetKey: "genres", label: "Genre" },
  { param: "decade", facetKey: "decades", label: "Decade" },
  { param: "format", facetKey: "formats", label: "Format" },
  { param: "album_type", facetKey: "album_types", label: "Type" },
  { param: "media", facetKey: "media", label: "Media" },
  { param: "country", facetKey: "countries", label: "Country" },
  { param: "source", facetKey: "sources", label: "Source" },
  { param: "lyrics", facetKey: "lyrics", label: "Lyrics" },
  { param: "tracks", facetKey: "tracks", label: "Tracks" },
] as const;
const FACET_PREVIEW_COUNT = 8;

type FacetParam = (typeof FACET_FIELDS)[number]["param"];

/**
 * Browse — slice the whole library across nine facets (genre, decade, format,
 * type, media, country, source, lyrics, tracks) with an optional artist/added
 * sort.
 *
 * A filter rail (checkbox groups + counts) beside the filtered album grid. All
 * state lives in the URL (`?genre=Rock&decade=2010s&sort=added&offset=48`), so
 * filters are bookmarkable and Back works. Filter logic is standard faceted: OR
 * within a facet, AND across facets — the backend does the matching. Counts are
 * whole-library totals (not re-derived against the active selection).
 */
export function BrowsePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { pageSize, setPageSize } = usePageSize();

  const filters: BrowseFilters = {
    genre: searchParams.getAll("genre"),
    decade: searchParams.getAll("decade"),
    format: searchParams.getAll("format"),
    album_type: searchParams.getAll("album_type"),
    media: searchParams.getAll("media"),
    country: searchParams.getAll("country"),
    source: searchParams.getAll("source"),
    lyrics: searchParams.getAll("lyrics"),
    tracks: searchParams.getAll("tracks"),
  };
  const sort: BrowseSort =
    searchParams.get("sort") === "added" ? "added" : "artist";
  const offset = Math.max(Number(searchParams.get("offset")) || 0, 0);

  // Per-group top-N expander — local only. An applied value beyond the top 8
  // stays reachable via its chip, so this state never needs to live in the URL.
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const facetsQuery = useBrowseFacets();
  const albumsQuery = useBrowseAlbums(filters, sort, pageSize, offset);

  const toggle = (param: FacetParam, value: string) => {
    const next = new URLSearchParams(searchParams);
    const current = next.getAll(param);
    next.delete(param);
    const after = current.includes(value)
      ? current.filter((v) => v !== value)
      : [...current, value];
    after.forEach((v) => next.append(param, v));
    next.delete("offset"); // any filter change returns to page 1
    setSearchParams(next);
  };

  const clearAll = () => {
    const next = new URLSearchParams();
    if (searchParams.get("sort") === "added") next.set("sort", "added");
    setSearchParams(next);
  };

  const setSort = (value: BrowseSort) => {
    const next = new URLSearchParams(searchParams);
    if (value === "added") next.set("sort", "added");
    else next.delete("sort");
    next.delete("offset"); // a re-sort returns to page 1
    setSearchParams(next);
  };

  // Post-page-change contract (spec §6): plain scroll to top + move focus to
  // the always-mounted count line in the PageHeader meta region.
  const countRef = useRef<HTMLSpanElement>(null);
  const railRef = useRef<HTMLElement>(null);
  useRailMaxHeight(railRef);
  const goToOffset = (value: number) => {
    const next = new URLSearchParams(searchParams);
    if (value <= 0) next.delete("offset");
    else next.set("offset", String(value));
    setSearchParams(next);
    window.scrollTo({ top: 0 });
    countRef.current?.focus({ preventScroll: true });
  };

  const activeChips = FACET_FIELDS.flatMap(({ param }) =>
    filters[param].map((value) => ({ param, value })),
  );

  const albums = albumsQuery.data?.items ?? [];
  const total = albumsQuery.data?.total ?? 0;
  const hasFilters = activeChips.length > 0;
  const isFetching = albumsQuery.isFetching;

  // Facet rail body: pending → error → grouped checkboxes, one branch each.
  const renderFacetRail = () => {
    if (facetsQuery.isPending) return <FilterRailSkeleton />;
    if (facetsQuery.isError)
      return (
        <ErrorState
          variant="inline"
          message="Couldn’t load filters."
          onRetry={() => void facetsQuery.refetch()}
        />
      );
    return (
      <div className="flex flex-col gap-5">
        {FACET_FIELDS.map(({ param, facetKey, label }) => {
          const values = facetsQuery.data?.[facetKey] ?? [];
          if (values.length === 0) return null;
          const shown = expanded[param]
            ? values
            : values.slice(0, FACET_PREVIEW_COUNT);
          return (
            <fieldset key={param} className="flex flex-col gap-1.5">
              <legend className="mb-1 text-sm font-medium">
                {label}
              </legend>
              {shown.map((fv) => {
                const id = `facet-${param}-${encodeURIComponent(fv.value)}`;
                return (
                  <div key={fv.value} className="flex items-center gap-2 text-sm">
                    <Checkbox
                      id={id}
                      checked={filters[param].includes(fv.value)}
                      onCheckedChange={() => toggle(param, fv.value)}
                    />
                    <label
                      htmlFor={id}
                      className="flex min-w-0 flex-1 cursor-pointer items-center gap-2"
                    >
                      <span
                        className={cn(
                          "flex-1 truncate",
                          param === "album_type" && "capitalize",
                        )}
                      >
                        {fv.value}
                      </span>
                      <span className="text-muted-foreground tabular-nums">
                        {fv.count}
                      </span>
                    </label>
                  </div>
                );
              })}
              {values.length > FACET_PREVIEW_COUNT && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="text-muted-foreground h-7 justify-start px-0"
                  onClick={() =>
                    setExpanded((prev) => ({
                      ...prev,
                      [param]: !prev[param],
                    }))
                  }
                >
                  {expanded[param]
                    ? "Show less"
                    : `Show all (${values.length})`}
                </Button>
              )}
            </fieldset>
          );
        })}
      </div>
    );
  };

  // Albums body: error → pending → no-results → past-the-end → grid, one branch
  // each. The two no-results EmptyStates differ only by whether filters ran.
  const renderAlbumsBody = () => {
    if (albumsQuery.isError)
      return (
        <ErrorState
          message="Couldn’t load albums. Check the backend and try again."
          onRetry={() => void albumsQuery.refetch()}
        />
      );
    if (albumsQuery.isPending)
      return (
        <PageSkeleton announce="Loading albums…">
          <AlbumsGridSkeleton count={Math.min(pageSize, 12)} />
        </PageSkeleton>
      );
    if (total === 0)
      return hasFilters ? (
        <EmptyState
          icon={Browse}
          title="No albums match these filters."
          body="Loosen or clear a filter to widen the net."
        />
      ) : (
        <EmptyState
          icon={MusicFallback}
          title="No albums in the library yet."
          body="Import some music to get started."
          action={
            <Button variant="outline" size="sm" asChild>
              <Link to="/import">Add music from a folder</Link>
            </Button>
          }
        />
      );
    if (albums.length === 0)
      return (
        <EmptyState
          bordered
          icon={Albums}
          title="This page is empty; the filters changed under it."
          action={
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => goToOffset(0)}
            >
              Back to first page
            </Button>
          }
        />
      );
    // total > 0 and this page has rows → the grid (+ optional pagination).
    return (
      <>
        <ul
          className={cn(
            GRID_CLASS,
            isFetching && "pointer-events-none opacity-60",
          )}
          aria-busy={isFetching}
        >
          {albums.map((album) => (
            <li key={album.id}>
              <AlbumCard
                album={album}
                from={{
                  label: "Browse",
                  to: `/browse?${searchParams.toString()}`,
                }}
              />
            </li>
          ))}
        </ul>
        {total > pageSize && (
          <div className="mt-6">
            <Pagination
              total={total}
              offset={offset}
              limit={pageSize}
              busy={isFetching}
              onOffsetChange={goToOffset}
            />
          </div>
        )}
      </>
    );
  };

  return (
    <PageBody>
      <PageHeader
        title="Browse"
        meta={
          albumsQuery.isPending ? (
            "Loading…"
          ) : (
            <span ref={countRef} tabIndex={-1}>
              {total.toLocaleString()} {total === 1 ? "album" : "albums"}
              {hasFilters ? " matching your filters" : " in your library"}.
            </span>
          )
        }
        // How much of the page you see is a property OF THE PAGE, so it sits
        // with the page's own controls rather than in the filter toolbar.
        // Same placement on Artists. SORT stays in the toolbar: it reorders
        // the results the filters produced, so it belongs with them.
        actions={
          total > PAGE_SIZE_OPTIONS[0] ? (
            <PageSizeSelect value={pageSize} onChange={setPageSize} />
          ) : undefined
        }
      />

      <div className="flex flex-col gap-6 md:flex-row">
        {/* NO-JUMP INVARIANT: the rail is sticky and self-contained — it never
            moves when the grid beside it changes height.
            GEOMETRY: the topbar is sticky and 4.5rem tall (Topbar.tsx: py-3 +
            h-12), and <main> pads 1.5rem (py-6) — so the rail rests at 6rem
            (top-24) and must cap at 100vh - 6rem - 1.5rem bottom gap so the
            whole scroll box always fits the viewport and wheel-over-rail
            scrolls the FILTERS, not the page. Change the topbar's height and
            these two constants move with it.
            The static max-h is only correct once the sticky PINS; before
            that (page at top, header in view) the rail starts lower and the
            static value would push its bottom — and the last facet group —
            below the viewport. useRailMaxHeight measures the real top and
            tightens max-height pre-pin; the class stays as the no-JS /
            first-paint fallback. */}
        {/* TAP TARGET: `overflow-y-auto` makes overflow-x compute to `auto`
            too, so this box clips its own content — and with no left padding
            its content box started at exactly the facet checkbox's left edge,
            cutting the 4px its 24px target reaches past the drawn box
            (decisions 40: the primitive gives every caller the target; a call
            site must not take it back). Measured 20.5×24.5 with 2 of 4
            corners; setting overflow to `visible` live restored 24.5×24.5,
            which is what named the cause.
            The fix is to move the CLIP 4px left while leaving the content
            where it is: `pl-1` puts the padding box 4px left of the content,
            `-ml-1` gives that 4px back to the layout, and `md:w-61` (15.25rem
            = w-60 + those 4px) keeps the CONTENT box 224px wide where the
            fixed width would otherwise have paid for the padding. Measured
            cost at every width from 320 to 1280: none — facet labels 190px
            and the album grid at x=518 (both unchanged), no horizontal
            scrollbar on the page or the rail, and the rail still scrolls
            vertically. `-ml-1 pl-1` alone — the remedy BACKLOG recorded — is
            what costs: labels 190→186 and the grid 518→514. */}
        <aside
          ref={railRef}
          className="thin-scrollbar max-h-72 -ml-1 shrink-0 overflow-y-auto pr-4 pl-1 md:sticky md:top-24 md:max-h-[calc(100vh-7.5rem)] md:w-61 md:self-start"
          aria-label="Filters"
        >
          {renderFacetRail()}
        </aside>

        <div className="flex min-w-0 flex-1 flex-col gap-4">
          {/* NO-JUMP INVARIANT: active-filter bar always rendered with a
              reserved min-h-8, so applying the FIRST filter fills it instead
              of inserting a new row that shoves the grid down. Chips wrap on
              the left; the sort select stays pinned right and always visible. */}
          <div className="flex min-h-8 flex-wrap items-center gap-2">
            <div className="flex flex-1 flex-wrap items-center gap-2">
              {hasFilters ? (
                <>
                  {activeChips.map(({ param, value }) => (
                    <Button
                      key={`${param}:${value}`}
                      type="button"
                      variant="secondary"
                      size="sm"
                      className="h-7 gap-1 px-2"
                      aria-label={`Remove ${value} filter`}
                      onClick={() => toggle(param, value)}
                    >
                      {value}
                      <Close className="size-3" aria-hidden="true" />
                    </Button>
                  ))}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={clearAll}
                  >
                    Clear all
                  </Button>
                </>
              ) : (
                <span className="text-muted-foreground text-sm">
                  Pick a filter to narrow your library.
                </span>
              )}
            </div>
            <div className="ml-auto flex shrink-0 items-center gap-2">
              {/* Same caret anatomy as PageSizeSelect: the UA arrow sits
                  flush against the edge, so draw our own with real inset. */}
              <span className="relative shrink-0">
                <select
                  aria-label="Sort albums"
                  className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-8 appearance-none rounded-md border px-2 pr-7 text-sm shadow-xs focus-visible:ring-[3px] focus-visible:outline-none"
                  value={sort}
                  onChange={(e) =>
                    setSort(e.target.value === "added" ? "added" : "artist")
                  }
                >
                  <option value="artist">A-Z (artist)</option>
                  <option value="added">Recently added</option>
                </select>
                <Expand
                  className="text-muted-foreground pointer-events-none absolute top-1/2 right-2 size-4 -translate-y-1/2"
                  aria-hidden="true"
                />
              </span>
              {total > pageSize && (
                <Pagination
                  compact
                  label="Pagination (top)"
                  total={total}
                  offset={offset}
                  limit={pageSize}
                  busy={isFetching}
                  onOffsetChange={goToOffset}
                />
              )}
            </div>
          </div>
          {/* NO-JUMP INVARIANT: reserved results height — filter flips never
              collapse the column under the sticky rail. */}
          <div className="min-h-[60vh]">
            {renderAlbumsBody()}
          </div>
        </div>
      </div>
    </PageBody>
  );
}

/** Placeholder facet rail while the facets load, so the rail doesn't pop in. */
function FilterRailSkeleton() {
  return (
    <div className="flex flex-col gap-5" aria-hidden="true">
      {FACET_FIELDS.map(({ param }) => (
        <div key={param} className="flex flex-col gap-2">
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      ))}
    </div>
  );
}
