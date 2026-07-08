import { useState } from "react";

import type { ImportSearch } from "@/api/useImport";
import { Info, Spinner } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

/** "Search for a different release" — a release URL/ID (the reliable escape from
 * beets' Various-Artists filter) or a forced-non-VA artist+album name search.
 * Submitting re-runs the lookup on the worker and re-parks this album. */
export function ReleaseSearchPanel({
  onSearch,
  busy,
  feedback,
  error,
}: {
  onSearch: (search: ImportSearch) => void;
  busy: boolean;
  feedback: string | null;
  error: boolean;
}) {
  const [releaseId, setReleaseId] = useState("");
  const [artist, setArtist] = useState("");
  const [album, setAlbum] = useState("");
  const [forceNonVa, setForceNonVa] = useState(true);

  const id = releaseId.trim();
  const canSearch =
    id.length > 0 || (artist.trim().length > 0 && album.trim().length > 0);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSearch || busy) return;
    // Release id wins (mirrors beets); otherwise the artist+album pair.
    onSearch(
      id
        ? { release_id: id, artist: null, album: null, force_non_va: forceNonVa }
        : {
            release_id: null,
            artist: artist.trim(),
            album: album.trim(),
            force_non_va: forceNonVa,
          },
    );
  }

  return (
    <form
      onSubmit={submit}
      className="border-border flex flex-col gap-3 rounded-xl border p-4"
    >
      <div className="flex items-center gap-1.5">
        <p className="text-sm font-medium">Search for a different release</p>
        <Popover>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="text-muted-foreground size-6"
              aria-label="Why paste a URL?"
            >
              <Info aria-hidden="true" />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-80 text-sm">
            When a single-artist album keeps matching a Various-Artists
            compilation, paste the exact release&rsquo;s MusicBrainz URL —
            artist URLs won&rsquo;t work; open the specific release and copy
            its link.
          </PopoverContent>
        </Popover>
      </div>
      <p className="text-muted-foreground text-xs">
        Paste a MusicBrainz release URL/ID or a Deezer album URL.
      </p>
      <Input
        value={releaseId}
        onChange={(e) => setReleaseId(e.target.value)}
        placeholder="https://musicbrainz.org/release/…"
        disabled={busy}
        aria-label="Release URL or ID"
      />
      <details className="text-sm">
        <summary className="text-muted-foreground cursor-pointer">
          …or search by name
        </summary>
        <div className="mt-3 flex flex-col gap-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="search-artist" className="text-sm font-medium">
                Artist
              </label>
              <Input
                id="search-artist"
                value={artist}
                onChange={(e) => setArtist(e.target.value)}
                disabled={busy}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="search-album" className="text-sm font-medium">
                Album
              </label>
              <Input
                id="search-album"
                value={album}
                onChange={(e) => setAlbum(e.target.value)}
                disabled={busy}
              />
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={forceNonVa}
              onChange={(e) => setForceNonVa(e.target.checked)}
              disabled={busy}
              className="size-4"
            />
            Not a Various-Artists compilation
          </label>
        </div>
      </details>
      {feedback && (
        <p className="text-muted-foreground text-sm" role="status">
          {feedback}
        </p>
      )}
      {error && (
        <p className="text-destructive text-sm" role="alert">
          Couldn’t run that search — try again.
        </p>
      )}
      <div>
        <Button
          type="submit"
          variant="outline"
          size="sm"
          disabled={!canSearch || busy}
        >
          {busy ? (
            <>
              <Spinner className="animate-spin" aria-hidden="true" /> Searching…
            </>
          ) : (
            "Search"
          )}
        </Button>
      </div>
    </form>
  );
}
