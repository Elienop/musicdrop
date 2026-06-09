import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
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
}

/** Poll the facet values (cached — absolute counts rarely change between visits). */
export function useBrowseFacets() {
  return useQuery<BrowseFacets>({
    queryKey: ["browse", "facets"],
    queryFn: async () => {
      const { data, error, response } = await client.GET("/api/browse/facets");
      if (error || !response.ok || !data) throw new Error("Failed to load facets");
      return data;
    },
    staleTime: 60_000,
  });
}

/** The filtered, paginated album page for the current filters + offset. */
export function useBrowseAlbums(
  filters: BrowseFilters,
  limit: number,
  offset: number,
) {
  return useQuery<AlbumPage>({
    queryKey: ["browse", "albums", filters, limit, offset],
    queryFn: async () => {
      const { data, error, response } = await client.GET("/api/browse/albums", {
        params: {
          query: {
            genre: filters.genre,
            decade: filters.decade,
            format: filters.format,
            limit,
            offset,
          },
        },
      });
      if (error || !response.ok || !data) throw new Error("Failed to load albums");
      return data;
    },
    // Keep the current grid visible while a filter toggle refetches.
    placeholderData: (prev) => prev,
  });
}
