import { Link } from "react-router";

import type { ExistingAlbum } from "@/api/useBank";
import { Panel } from "@/components/import/DuplicateReview";
import { SectionLabel } from "@/components/system/SectionLabel";
import { Button } from "@/components/ui/button";

/**
 * The up-front "Already in your library" comparison — each colliding library
 * copy as the duplicate screen's rich panel PLUS its own tracklist, so
 * Skip / Keep both / Replace / Merge is decidable from what the copy actually
 * holds ("your copy: 1 track" beside an incoming 10-track release). `blurb`
 * differs per flow: the bank resolves inline below this section; the live
 * import resolves on beets' duplicate prompt after Apply.
 */
export function AlreadyInLibrary({
  existing,
  blurb,
}: {
  existing: ExistingAlbum[];
  blurb: string;
}) {
  return (
    <section aria-label="Already in your library" className="flex flex-col gap-3">
      <SectionLabel>Already in your library</SectionLabel>
      <p className="text-muted-foreground text-sm">{blurb}</p>
      <ul className="flex flex-col gap-4">
        {existing.map((album) => (
          <li key={album.album_id} className="flex flex-col gap-2">
            <Panel
              id={`upfront-${album.album_id}`}
              heading="Already in library"
              album={album}
              coverUrl={`/api/albums/${album.album_id}/cover?size=thumb`}
              coverAssetKey={`album:${album.album_id}`}
            />
            {album.tracks.length > 0 && <ExistingTracklist tracks={album.tracks} />}
            <div>
              <Button variant="outline" size="sm" asChild>
                <Link to={`/albums/${album.album_id}`}>View</Link>
              </Button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** The library copy's own tracks: number · title · quality. */
function ExistingTracklist({ tracks }: { tracks: ExistingAlbum["tracks"] }) {
  return (
    <ol className="border-border divide-border divide-y rounded-lg border text-sm">
      {tracks.map((t, i) => (
        <li key={i} className="flex items-center gap-3 px-3 py-1.5">
          <span className="text-muted-foreground w-6 shrink-0 text-right font-mono text-xs">
            {t.track ?? "-"}
          </span>
          <span className="min-w-0 flex-1 truncate">{t.title ?? "Untitled"}</span>
          <span className="text-muted-foreground shrink-0 text-xs">
            {[t.format, t.bitrate_kbps ? `${t.bitrate_kbps} kbps` : null]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </li>
      ))}
    </ol>
  );
}
