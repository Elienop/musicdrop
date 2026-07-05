import { useEffect, useState } from "react";

import type { SearchTrack } from "@/api/useSearch";
import { useTypedSearch } from "@/api/useSearch";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { formatDuration } from "@/lib/format";

/** What a caller receives when a search result is picked. */
export interface PickedTrack {
  item_id: number;
  title: string;
  artist: string;
  album: string;
}

// A generous single-page cap — the picker is a "find one track" affordance, not
// a browser, so the first page of hits is plenty; the user refines the query
// instead of paging.
const RESULT_LIMIT = 25;

/**
 * Playlist-agnostic "find a library track" dialog: a debounced search box over
 * `GET /api/search` (`type=tracks`) listing hits, each with a Select button that
 * fires `onPick` and closes. It knows nothing about playlists (or what the pick
 * is for) — the detail page uses it to resolve a pending entry, and the import
 * flow (Task 5) reuses it for the same shape.
 */
export function TrackMatchPicker({
  open,
  onOpenChange,
  onPick,
  title = "Match a track",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPick: (track: PickedTrack) => void;
  title?: string;
}) {
  const [query, setQuery] = useState("");
  // Debounce keystrokes (~300ms) so a burst of typing fires one search, not one
  // per character. The visible input stays fully controlled; only the value we
  // search on trails behind.
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query), 300);
    return () => clearTimeout(timer);
  }, [query]);

  // Reset the box whenever the dialog closes so it reopens clean.
  function close(next: boolean) {
    if (!next) {
      setQuery("");
      setDebounced("");
    }
    onOpenChange(next);
  }

  function pick(hit: SearchTrack) {
    onPick({
      item_id: hit.id,
      title: hit.title,
      artist: hit.artist,
      album: hit.album,
    });
    close(false);
  }

  const search = useTypedSearch(debounced, "tracks", RESULT_LIMIT, 0);
  const trimmed = debounced.trim();
  const results = search.data?.tracks ?? [];

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            Search your library and pick the track this entry should point to.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Input
            aria-label="Search library tracks"
            placeholder="Search by title, artist, or album…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
          <div className="max-h-72 overflow-y-auto">
            {trimmed.length === 0 ? (
              <p className="text-muted-foreground py-6 text-center text-sm">
                Start typing to search your library.
              </p>
            ) : search.isError ? (
              <p className="text-destructive py-6 text-center text-sm" role="alert">
                Search failed. Try again.
              </p>
            ) : results.length === 0 && !search.isPending ? (
              <p className="text-muted-foreground py-6 text-center text-sm">
                No tracks found.
              </p>
            ) : (
              <ul className="flex flex-col">
                {results.map((hit) => (
                  <li
                    key={hit.id}
                    className="flex items-center justify-between gap-3 border-b py-2 last:border-b-0"
                  >
                    <div className="flex min-w-0 flex-col">
                      <span className="truncate font-medium">{hit.title}</span>
                      <span className="text-muted-foreground truncate text-sm">
                        {hit.artist}
                        {hit.album && (
                          <>
                            <span aria-hidden="true"> &middot; </span>
                            {hit.album}
                          </>
                        )}
                        {hit.duration_seconds != null && (
                          <>
                            <span aria-hidden="true"> &middot; </span>
                            {formatDuration(hit.duration_seconds)}
                          </>
                        )}
                      </span>
                    </div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="shrink-0"
                      aria-label={`Select ${hit.title}`}
                      onClick={() => pick(hit)}
                    >
                      Select
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
