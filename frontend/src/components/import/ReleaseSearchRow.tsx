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

/** The compact "different release" form the ReviewControlBar reveals: a
 * release URL/ID (the reliable escape from beets' Various-Artists filter) or
 * a forced-non-VA artist+album search, all inline. Submitting re-runs the
 * lookup on the worker and re-parks the album. */
export function ReleaseSearchRow({
  onSearch,
  busy,
  feedback,
  error,
  formId,
}: {
  onSearch: (search: ImportSearch) => void;
  busy: boolean;
  feedback: string | null;
  error: boolean;
  formId: string;
}) {
  const [releaseId, setReleaseId] = useState("");
  const [artist, setArtist] = useState("");
  const [album, setAlbum] = useState("");
  const [forceNonVa, setForceNonVa] = useState(true);

  const id = releaseId.trim();
  const canSearch =
    id.length > 0 || (artist.trim().length > 0 && album.trim().length > 0);

  function submit(e: React.SubmitEvent) {
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
      id={formId}
      onSubmit={submit}
      aria-label="Search for a different release"
      className="flex flex-col gap-2"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={releaseId}
          onChange={(e) => setReleaseId(e.target.value)}
          placeholder="MusicBrainz/Deezer release URL or ID"
          aria-label="Release URL or ID"
          disabled={busy}
          className="h-8 min-w-60 flex-1"
        />
        <Input
          value={artist}
          onChange={(e) => setArtist(e.target.value)}
          placeholder="Artist"
          aria-label="Artist"
          disabled={busy}
          className="h-8 w-full sm:w-40"
        />
        <Input
          value={album}
          onChange={(e) => setAlbum(e.target.value)}
          placeholder="Album"
          aria-label="Album"
          disabled={busy}
          className="h-8 w-full sm:w-44"
        />
        <Button type="submit" variant="outline" size="sm" disabled={!canSearch || busy}>
          {busy ? (
            <>
              <Spinner className="animate-spin" aria-hidden="true" /> Searching…
            </>
          ) : (
            "Search"
          )}
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={forceNonVa}
            onChange={(e) => setForceNonVa(e.target.checked)}
            disabled={busy}
            className="size-4"
          />
          Not a compilation
        </label>
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
            If the album keeps matching a Various Artists compilation, paste
            the exact release URL. Artist pages won&rsquo;t work; open the
            release and copy its link.
          </PopoverContent>
        </Popover>
        {feedback && (
          <p className="text-muted-foreground text-sm" role="status">
            {feedback}
          </p>
        )}
        {error && (
          <p className="text-destructive text-sm" role="alert">
            Couldn’t run that search. Try again.
          </p>
        )}
      </div>
    </form>
  );
}
