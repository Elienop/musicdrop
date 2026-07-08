import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import { unwrap } from "@/api/lib";
import type { components } from "@/api/schema";

/** Available filter values + counts (`GET /api/browse/facets`, generated). */
export type BrowseFacets = components["schemas"]["BrowseFacets"];
/** One facet option + its whole-library album count (generated). */
export type FacetValue = components["schemas"]["FacetValue"];
/** A page of filtered albums (`GET /api/browse/albums`, generated). */
export type AlbumPage = components["schemas"]["AlbumPage"];

/** The active filter selection, one string[] per facet (OR within, AND across). */
export interface BrowseFilters {
  genre: string[];
  decade: string[];
  format: string[];
  album_type: string[];
  media: string[];
  country: string[];
  source: string[];
  lyrics: string[];
  tracks: string[];
}

/** Album ordering for the browse grid. */
export type BrowseSort = "artist" | "added";

/** Poll the facet values (cached — absolute counts rarely change between visits). */
export function useBrowseFacets() {
  return useQuery<BrowseFacets>({
    queryKey: ["browse", "facets"],
    queryFn: async () =>
      unwrap(await client.GET("/api/browse/facets"), "Failed to load facets"),
    staleTime: 60_000,
  });
}

/** The filtered, paginated album page for the current filters + sort + offset. */
export function useBrowseAlbums(
  filters: BrowseFilters,
  sort: BrowseSort,
  limit: number,
  offset: number,
) {
  return useQuery<AlbumPage>({
    queryKey: ["browse", "albums", filters, sort, limit, offset],
    queryFn: async () =>
      unwrap(
        await client.GET("/api/browse/albums", {
          params: {
            query: {
              genre: filters.genre,
              decade: filters.decade,
              format: filters.format,
              album_type: filters.album_type,
              media: filters.media,
              country: filters.country,
              source: filters.source,
              lyrics: filters.lyrics,
              tracks: filters.tracks,
              sort,
              limit,
              offset,
            },
          },
        }),
        "Failed to load albums",
      ),
    // Keep the current grid visible while a filter toggle refetches.
    placeholderData: (prev) => prev,
  });
}
