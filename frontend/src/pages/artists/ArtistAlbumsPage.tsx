import {
  ChevronLeft,
  ChevronRight,
  Disc3,
  Image as ImageIcon,
  Loader2,
  UploadCloud,
} from "lucide-react";
import { useState } from "react";
import { useParams, useSearchParams } from "react-router";

import { useAlbums } from "@/api/useAlbums";
import {
  useArtistArtBackfillStatus,
  useArtistArtSettings,
  useStartArtistArtApply,
} from "@/api/useArtistArt";
import { useArtistImageSettings } from "@/api/useArtistImage";
import {
  AlbumCard,
  AlbumsGridSkeleton,
  BackLink,
  ErrorState,
  GRID_CLASS,
} from "@/components/albums/album-grid";
import { ArtistImage } from "@/components/artists/ArtistImage";
import { ArtistImageEditPanel } from "@/components/artists/ArtistImageEditPanel";
import { ReorganizeControl } from "@/components/reorganize/ReorganizeControl";
import { useAutoDismiss } from "@/lib/useAutoDismiss";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ArtistAlbumsPageProps {
  /** Page size. Defaults to 50; overridable for tests. */
  initialLimit?: number;
}

/** `decodeURIComponent` throws `URIError` on malformed input (e.g. a lone "%").
 * Fall back to the raw param so a bad URL renders gracefully rather than
 * crashing the page. */
function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function ArtistAlbumsPage({ initialLimit = 50 }: ArtistAlbumsPageProps) {
  const [limit] = useState(initialLimit);
  // The artist is fixed by the ROUTE (`/artists/:artistName`), so a different
  // artist is a fresh mount — no in-place filter-change/offset-reset effect.
  const { artistName } = useParams<{ artistName: string }>();
  const artist = safeDecode(artistName ?? "");
  // Display fallback for the (latent) blank-name case; the API filter still
  // uses the real decoded `artist`. Backend filters blanks, so this is just
  // belt-and-suspenders symmetry with the roster card.
  const displayName = artist || "Unknown artist";

  // Offset lives in the URL (`?offset=N`) so the grid page is bookmarkable and
  // restored when navigating back from album detail.
  const [searchParams, setSearchParams] = useSearchParams();
  const offset = Math.max(0, Number(searchParams.get("offset") ?? "0") || 0);

  const [editingImage, setEditingImage] = useState(false);
  const [imageVersion, setImageVersion] = useState(0);
  const imagesEnabled = useArtistImageSettings().data?.enabled ?? false;
  // The single write-to-library toggle drives BOTH fetch + write; when on, the
  // header gains a per-artist "Apply to library" action with marching progress.
  const writeEnabled = useArtistArtSettings().data?.enabled ?? false;

  const { data, isPending, isError, isFetching, refetch } = useAlbums({
    limit,
    offset,
    artist,
  });

  const total = data?.total ?? 0;
  const hasPagination = total > limit;
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  function goToOffset(next: number) {
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        if (next <= 0) {
          params.delete("offset");
        } else {
          params.set("offset", String(next));
        }
        return params;
      },
      { replace: false },
    );
    if (typeof window !== "undefined") {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  return (
    <section
      className="flex flex-col gap-6"
      aria-label={`Albums by ${displayName}`}
    >
      <div className="flex flex-col gap-6">
        <BackLink to="/" label="Artists" />
        {/* Poster + name row, mirroring the album-detail header. The poster is
            decorative — the adjacent <h2> already names the artist. */}
        <div className="flex flex-col gap-6 sm:flex-row sm:items-center">
          <ArtistImage
            name={displayName}
            decorative
            version={imageVersion}
            className="size-40 shrink-0 rounded-xl shadow-sm"
            monogramClassName="text-6xl"
          />
          <div className="flex min-w-0 flex-col gap-1">
            <h2 className="text-3xl font-semibold tracking-tight break-words">
              {displayName}
            </h2>
            {/* Live region mounted unconditionally so assistive tech can
                observe it before the count arrives; only the text toggles. */}
            <p
              className="text-muted-foreground min-h-5 text-sm"
              aria-live="polite"
            >
              {!isPending && !isError && total > 0
                ? `${total.toLocaleString()} ${total === 1 ? "album" : "albums"}`
                : ""}
            </p>
            {/* Per-artist maintenance actions, below the title. Each control is a
                [messages-on-top, button-below] unit, bottom-aligned so the
                buttons line up in a row while any running/terminal message sits
                in the top row above them (not crammed onto the button line). */}
            <div className="border-border mt-3 flex flex-wrap items-end gap-x-6 gap-y-3 border-t pt-3">
              {imagesEnabled && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setEditingImage((v) => !v)}
                  aria-label="Edit artist image"
                >
                  <ImageIcon className="size-4" /> Image
                </Button>
              )}
              {writeEnabled && <ArtistArtStatus displayName={displayName} />}
              <ReorganizeControl scope={{ scope: "artist", artist: displayName }} />
            </div>
          </div>
        </div>
      </div>

      {editingImage && (
        <ArtistImageEditPanel
          name={displayName}
          onSaved={() => setImageVersion((v) => v + 1)}
          onClose={() => setEditingImage(false)}
        />
      )}

      {isPending ? (
        <>
          <p className="sr-only" role="status">
            Loading albums&hellip;
          </p>
          <AlbumsGridSkeleton count={Math.min(limit, 18)} />
        </>
      ) : isError ? (
        <ErrorState onRetry={() => void refetch()} />
      ) : data.items.length === 0 ? (
        // total === 0: the artist genuinely has no albums. total > 0 with an
        // empty page means the offset is past the end (stale/hand-crafted URL)
        // — the artist DOES have albums, so don't claim otherwise.
        total === 0 ? (
          <ArtistEmptyState artist={displayName} />
        ) : (
          <OutOfRangePage onFirstPage={() => goToOffset(0)} />
        )
      ) : (
        <>
          <ul
            className={cn(
              GRID_CLASS,
              isFetching && "pointer-events-none opacity-60 transition-opacity",
            )}
            aria-busy={isFetching}
          >
            {data.items.map((album) => (
              <li key={album.id}>
                <AlbumCard album={album} />
              </li>
            ))}
          </ul>

          {hasPagination && (
            <nav
              className="flex items-center justify-between gap-4"
              aria-label="Albums pagination"
            >
              <Button
                variant="outline"
                size="sm"
                disabled={!canPrev || isFetching}
                onClick={() => goToOffset(Math.max(0, offset - limit))}
              >
                <ChevronLeft />
                Previous
              </Button>
              <span
                className="text-muted-foreground flex items-center gap-2 text-sm"
                aria-live="polite"
              >
                {isFetching && (
                  <Loader2
                    className="size-3.5 animate-spin"
                    aria-hidden="true"
                  />
                )}
                <span>
                  {offset + 1}&ndash;{Math.min(offset + limit, total)} of{" "}
                  {total}
                </span>
              </span>
              <Button
                variant="outline"
                size="sm"
                disabled={!canNext || isFetching}
                onClick={() => goToOffset(offset + limit)}
              >
                Next
                <ChevronRight />
              </Button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}

/** The per-artist "Apply to library" action + marching progress, mirroring the
 * album `LyricsStatus`. This artist owns the single backfill slot when the
 * running job's scoped `artist` equals our `displayName`; otherwise the button
 * just kicks off a per-artist apply (force-overwrite). */
function ArtistArtStatus({ displayName }: { displayName: string }) {
  const status = useArtistArtBackfillStatus();
  const start = useStartArtistArtApply(displayName);
  const job = status.data;
  const isThisArtist = job?.artist === displayName;
  const runningThis = job?.phase === "running" && isThisArtist;
  const otherRunning = job?.phase === "running" && !isThisArtist;
  const terminalThis =
    isThisArtist && (job?.phase === "done" || job?.phase === "stopped");
  // The finished tally fades ~8s after completion instead of lingering.
  const showTally = useAutoDismiss(terminalThis, job?.job_id ?? null);

  return (
    <div className="flex flex-col items-start gap-2">
      {/* Messages (top row): running progress / terminal tally / errors. */}
      {runningThis && job && (
        <span
          className="text-muted-foreground flex items-center gap-2 text-sm"
          role="status"
        >
          <Loader2 className="size-4 shrink-0 animate-spin" aria-hidden="true" />
          Writing artist art… {job.processed} / {job.total}
        </span>
      )}
      {!runningThis && showTally && job && (
        <span className="text-muted-foreground text-sm" role="status">
          {job.written} written · {job.skipped} skipped · {job.failed} failed
        </span>
      )}
      {otherRunning && (
        <span className="text-muted-foreground text-sm">
          another artist-art job is running
        </span>
      )}
      {start.isError && (
        <span className="text-destructive text-sm" role="alert">
          {(start.error as Error).message}
        </span>
      )}

      {/* Button (bottom row) — hidden while this artist's job runs. */}
      {!runningThis && (
        <Button
          variant="outline"
          size="sm"
          onClick={() => start.mutate()}
          disabled={start.isPending || otherRunning}
          aria-label="Write artist art"
        >
          {start.isPending ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden="true" />{" "}
              Starting…
            </>
          ) : (
            <>
              <UploadCloud className="size-4" /> Write artist art
            </>
          )}
        </Button>
      )}
    </div>
  );
}

/** Shown when `?offset=` points past the end of an artist that DOES have
 * albums. Offers a one-click return to the first page. */
function OutOfRangePage({ onFirstPage }: { onFirstPage: () => void }) {
  return (
    <div className="border-border flex flex-col items-center gap-3 rounded-xl border border-dashed py-16 text-center">
      <Disc3 className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">Nothing on this page</p>
        <p className="text-muted-foreground text-sm">
          This page is past the end of the list.
        </p>
      </div>
      <Button variant="outline" size="sm" onClick={onFirstPage}>
        Back to first page
      </Button>
    </div>
  );
}

/** Empty state for an artist with no albums (e.g. a stale bookmark). Names the
 * artist and offers an escape back to the roster. */
function ArtistEmptyState({ artist }: { artist: string }) {
  return (
    <div className="border-border flex flex-col items-center gap-3 rounded-xl border border-dashed py-16 text-center">
      <Disc3 className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">No albums for {artist}</p>
        <p className="text-muted-foreground text-sm">
          Nothing in your library is filed under this artist.
        </p>
      </div>
      <BackLink to="/" label="Artists" />
    </div>
  );
}
