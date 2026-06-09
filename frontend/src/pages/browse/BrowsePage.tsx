import { X } from "lucide-react";
import { useSearchParams } from "react-router";

import {
  type BrowseFilters,
  useBrowseAlbums,
  useBrowseFacets,
} from "@/api/useBrowse";
import {
  AlbumCard,
  AlbumsGridSkeleton,
  ErrorState,
  GRID_CLASS,
} from "@/components/albums/album-grid";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 48;

const FACET_FIELDS = [
  { param: "genre", label: "Genre" },
  { param: "decade", label: "Decade" },
  { param: "format", label: "Format" },
] as const;

type FacetParam = (typeof FACET_FIELDS)[number]["param"];

/**
 * Browse — slice the whole library by genre · decade · format.
 *
 * A filter rail (checkbox groups + counts) beside the filtered album grid. All
 * state lives in the URL (`?genre=Rock&decade=2010s&format=FLAC&offset=48`), so
 * filters are bookmarkable and Back works. Filter logic is standard faceted: OR
 * within a facet, AND across facets — the backend does the matching. Counts are
 * whole-library totals (not re-derived against the active selection).
 */
export function BrowsePage() {
  const [searchParams, setSearchParams] = useSearchParams();

  const filters: BrowseFilters = {
    genre: searchParams.getAll("genre"),
    decade: searchParams.getAll("decade"),
    format: searchParams.getAll("format"),
  };
  const offset = Math.max(Number(searchParams.get("offset")) || 0, 0);

  const facetsQuery = useBrowseFacets();
  const albumsQuery = useBrowseAlbums(filters, PAGE_SIZE, offset);

  const facetGroups = {
    genre: facetsQuery.data?.genres ?? [],
    decade: facetsQuery.data?.decades ?? [],
    format: facetsQuery.data?.formats ?? [],
  };

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

  const clearAll = () => setSearchParams(new URLSearchParams());

  const goToOffset = (value: number) => {
    const next = new URLSearchParams(searchParams);
    if (value <= 0) next.delete("offset");
    else next.set("offset", String(value));
    setSearchParams(next);
    window.scrollTo({ top: 0 });
  };

  const activeChips = FACET_FIELDS.flatMap(({ param }) =>
    filters[param].map((value) => ({ param, value })),
  );

  const albums = albumsQuery.data?.items ?? [];
  const total = albumsQuery.data?.total ?? 0;
  const hasFilters = activeChips.length > 0;
  const isFetching = albumsQuery.isFetching;

  return (
    <section aria-label="Browse" className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">Browse</h2>
        <p aria-live="polite" className="text-muted-foreground min-h-5 text-sm">
          {albumsQuery.isPending
            ? "Loading…"
            : `${total.toLocaleString()} ${total === 1 ? "album" : "albums"}${
                hasFilters ? " matching your filters" : " in your library"
              }.`}
        </p>
      </header>

      <div className="flex flex-col gap-6 md:flex-row">
        <aside
          className="max-h-72 shrink-0 overflow-y-auto md:sticky md:top-6 md:max-h-[calc(100vh-3rem)] md:w-56 md:self-start"
          aria-label="Filters"
        >
          {facetsQuery.isPending ? (
            <FilterRailSkeleton />
          ) : facetsQuery.isError ? (
            <p className="text-muted-foreground text-sm">
              Couldn’t load filters.{" "}
              <button
                type="button"
                className="text-foreground underline"
                onClick={() => void facetsQuery.refetch()}
              >
                Retry
              </button>
            </p>
          ) : (
            <div className="flex flex-col gap-5">
              {FACET_FIELDS.map(({ param, label }) => {
                const values = facetGroups[param];
                if (values.length === 0) return null;
                return (
                  <fieldset key={param} className="flex flex-col gap-1.5">
                    <legend className="mb-1 text-sm font-medium">
                      {label}
                    </legend>
                    {values.map((fv) => (
                      <label
                        key={fv.value}
                        className="flex cursor-pointer items-center gap-2 text-sm"
                      >
                        <input
                          type="checkbox"
                          className="accent-primary size-4"
                          checked={filters[param].includes(fv.value)}
                          onChange={() => toggle(param, fv.value)}
                        />
                        <span className="flex-1 truncate">{fv.value}</span>
                        <span className="text-muted-foreground tabular-nums">
                          {fv.count}
                        </span>
                      </label>
                    ))}
                  </fieldset>
                );
              })}
            </div>
          )}
        </aside>

        <div className="flex min-w-0 flex-1 flex-col gap-4">
          {/* Active-filter bar — always rendered with a reserved height, so
              applying the FIRST filter fills it instead of inserting a new row
              that shoves the grid down. The sticky rail to the left never moves. */}
          <div className="flex min-h-8 flex-wrap items-center gap-2">
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
                    <X className="size-3" aria-hidden="true" />
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
          <div className="min-h-[60vh]">
            {albumsQuery.isError ? (
              <ErrorState onRetry={() => void albumsQuery.refetch()} />
            ) : albumsQuery.isPending ? (
              <AlbumsGridSkeleton count={Math.min(PAGE_SIZE, 12)} />
            ) : total === 0 ? (
              <p className="text-muted-foreground text-sm">
                {hasFilters
                  ? "No albums match these filters."
                  : "No albums in the library yet."}
              </p>
            ) : albums.length === 0 ? (
              // total > 0 but this page is empty → the offset is past the end.
              <div className="flex flex-col items-start gap-3">
                <p className="text-muted-foreground text-sm">
                  This page is empty — the filters changed under it.
                </p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => goToOffset(0)}
                >
                  Back to first page
                </Button>
              </div>
            ) : (
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
                {total > PAGE_SIZE && (
                  <nav
                    aria-label="Browse pagination"
                    className="mt-6 flex items-center justify-between gap-2"
                  >
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={offset === 0 || isFetching}
                      onClick={() => goToOffset(offset - PAGE_SIZE)}
                    >
                      Previous
                    </Button>
                    <span className="text-muted-foreground text-sm tabular-nums">
                      {(offset + 1).toLocaleString()}–
                      {Math.min(offset + PAGE_SIZE, total).toLocaleString()} of{" "}
                      {total.toLocaleString()}
                    </span>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={offset + PAGE_SIZE >= total || isFetching}
                      onClick={() => goToOffset(offset + PAGE_SIZE)}
                    >
                      Next
                    </Button>
                  </nav>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </section>
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
