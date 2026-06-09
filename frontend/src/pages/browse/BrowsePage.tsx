import { X } from "lucide-react";
import { useSearchParams } from "react-router";

import {
  type BrowseFilters,
  useBrowseAlbums,
  useBrowseFacets,
} from "@/api/useBrowse";
import { AlbumCard, GRID_CLASS } from "@/components/albums/album-grid";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

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
 * A filter rail (checkbox groups + counts) beside the filtered album grid.
 * All state lives in the URL (`?genre=Rock&decade=2010s&format=FLAC&offset=48`),
 * so filters are bookmarkable and Back works. Filter logic is standard faceted:
 * OR within a facet, AND across facets — the backend does the matching.
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

  const setOffset = (value: number) => {
    const next = new URLSearchParams(searchParams);
    if (value <= 0) next.delete("offset");
    else next.set("offset", String(value));
    setSearchParams(next);
  };

  const activeChips = FACET_FIELDS.flatMap(({ param }) =>
    filters[param].map((value) => ({ param, value })),
  );

  const albums = albumsQuery.data?.items ?? [];
  const total = albumsQuery.data?.total ?? 0;
  const hasFilters = activeChips.length > 0;

  return (
    <section aria-label="Browse" className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">Browse</h2>
        <p className="text-muted-foreground text-sm">
          {total} {total === 1 ? "album" : "albums"}
          {hasFilters ? " matching your filters" : " in your library"}.
        </p>
      </header>

      {hasFilters && (
        <div className="flex flex-wrap items-center gap-2">
          {activeChips.map(({ param, value }) => (
            <Button
              key={`${param}:${value}`}
              type="button"
              variant="secondary"
              size="sm"
              className="h-7 gap-1 px-2"
              onClick={() => toggle(param, value)}
            >
              {value}
              <X className="size-3" aria-hidden="true" />
              <span className="sr-only">Remove {value} filter</span>
            </Button>
          ))}
          <Button type="button" variant="ghost" size="sm" onClick={clearAll}>
            Clear all
          </Button>
        </div>
      )}

      <div className="flex flex-col gap-6 md:flex-row">
        <aside className="shrink-0 md:w-56" aria-label="Filters">
          <div className="flex flex-col gap-5">
            {FACET_FIELDS.map(({ param, label }) => {
              const values = facetGroups[param];
              if (values.length === 0) return null;
              return (
                <fieldset key={param} className="flex flex-col gap-1.5">
                  <legend className="mb-1 text-sm font-medium">{label}</legend>
                  {values.map((fv) => {
                    const checked = filters[param].includes(fv.value);
                    return (
                      <label
                        key={fv.value}
                        className="flex cursor-pointer items-center gap-2 text-sm"
                      >
                        <input
                          type="checkbox"
                          className="accent-primary size-4"
                          checked={checked}
                          onChange={() => toggle(param, fv.value)}
                        />
                        <span className="flex-1 truncate">{fv.value}</span>
                        <span className="text-muted-foreground tabular-nums">
                          {fv.count}
                        </span>
                      </label>
                    );
                  })}
                </fieldset>
              );
            })}
          </div>
        </aside>

        <div className="min-w-0 flex-1">
          {albums.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              {hasFilters
                ? "No albums match these filters."
                : "No albums in the library yet."}
            </p>
          ) : (
            <>
              <ul className={GRID_CLASS}>
                {albums.map((album) => (
                  <li key={album.id}>
                    <AlbumCard album={album} />
                  </li>
                ))}
              </ul>
              {total > PAGE_SIZE && (
                <div className="mt-6 flex items-center justify-between gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={offset === 0}
                    onClick={() => setOffset(offset - PAGE_SIZE)}
                  >
                    Previous
                  </Button>
                  <span className="text-muted-foreground text-sm tabular-nums">
                    {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
                  </span>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={offset + PAGE_SIZE >= total}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    Next
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {hasFilters && (
        <Badge variant="outline" className="sr-only">
          {activeChips.length} active filters
        </Badge>
      )}
    </section>
  );
}
