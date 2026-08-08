import { useEffect, useRef, useState } from "react";
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
  GRID_CLASS,
} from "@/components/albums/album-grid";
import { ArtistImage } from "@/components/artists/ArtistImage";
import { ArtistImageEditPanel } from "@/components/artists/ArtistImageEditPanel";
import { Albums, Cover, SaveArt, Spinner } from "@/components/icons";
import { ReorganizeControl } from "@/components/reorganize/ReorganizeControl";
import { EmptyState } from "@/components/system/EmptyState";
import { IconAction } from "@/components/system/IconAction";
import { SectionLabel } from "@/components/system/SectionLabel";
import { ErrorState } from "@/components/system/ErrorState";
import { PageBody } from "@/components/system/PageHeader";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { PAGE_SIZE, Pagination } from "@/components/system/Pagination";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { DeleteArtistAction } from "@/pages/artists/DeleteArtistAction";

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

export function ArtistAlbumsPage() {
  // The artist is fixed by the ROUTE (`/artists/:artistName`), so a different
  // artist is a fresh mount — no in-place filter-change/offset-reset effect.
  const { artistName } = useParams<{ artistName: string }>();
  const artist = safeDecode(artistName ?? "");
  // Display fallback for the (latent) blank-name case; the API filter still
  // uses the real decoded `artist`.
  const displayName = artist || "Unknown artist";

  // Offset lives in the URL (`?offset=N`) so the grid page is bookmarkable and
  // restored when navigating back from album detail.
  const [searchParams, setSearchParams] = useSearchParams();
  const offset = Math.max(0, Number(searchParams.get("offset") ?? "0") || 0);

  const [editingImage, setEditingImage] = useState(false);
  const [imageVersion, setImageVersion] = useState(0);
  // Spec §4 disclosure pattern (the AlbumDetailPage idiom): opening the
  // inline panel moves focus into it; closing from INSIDE the panel (its
  // Cancel/Done unmount the focused button) returns focus to the toggle.
  const imagePanelRef = useRef<HTMLDivElement>(null);
  const imageToggleRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (editingImage) imagePanelRef.current?.focus();
  }, [editingImage]);
  const closeImagePanel = () => {
    setEditingImage(false);
    imageToggleRef.current?.focus();
  };
  const imageSettings = useArtistImageSettings();
  // The single write-to-library toggle drives BOTH fetch + write; when on, the
  // header gains a per-artist "Apply to library" action with marching progress.
  const artSettings = useArtistArtSettings();
  const imagesEnabled = imageSettings.data?.enabled ?? false;
  const writeEnabled = artSettings.data?.enabled ?? false;
  const { data, isPending, isError, isFetching, refetch } = useAlbums({
    limit: PAGE_SIZE,
    offset,
    artist,
  });

  const total = data?.total ?? 0;

  // Post-page-change contract (spec §6): plain scroll to top + move focus to
  // the always-mounted count line in the PageHeader meta region, so focus is
  // never stranded on a control and SR users hear where they landed.
  const countRef = useRef<HTMLSpanElement>(null);
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
    window.scrollTo({ top: 0 });
    countRef.current?.focus({ preventScroll: true });
  }

  return (
    <PageBody>
      <BackLink to="/artists" label="Artists" />
      {/* PROTOTYPE v2: two-column layout — the left rail is dedicated to the
          artist (tall portrait dissolving into the page background, name +
          count inside the fade, actions stacked below); the albums grid moves
          up beside it on the right. --surface-base === --background, so the
          fade lands exactly on the page color and the portrait melts into
          the page itself (no hero card / backdrop needed). */}
      <div className="flex flex-col gap-8 lg:flex-row lg:items-start">
        <aside className="w-96 shrink-0 max-lg:mx-auto lg:sticky lg:top-20">
          {/* The rail is ONE bordered unit (Koito card treatment): the
              hairline wraps portrait + name + stats + divider + actions
              together. The fade dissolves into the panel interior (page
              color), so the melt survives inside the frame. */}
          <div className="border-border overflow-hidden rounded-xl border pb-3">
          <div className="relative">
            <ArtistImage
              name={displayName}
              decorative
              version={imageVersion}
              size="thumb"
              className="aspect-square w-full"
              monogramClassName="text-8xl"
            />
            {/* Eased smoothstep fade (the Koito treatment): spans the bottom
                60% of the portrait with zero slope at both ends, so there is
                no visible start line and the photo melts into the page. */}
            <div
              aria-hidden="true"
              className="fade-bottom-to-base absolute inset-0"
            />
          </div>
          {/* ...and the black keeps going ~20% past the edge: this strip is
              plain page background (=== the fade color), so the image melts
              into it seamlessly and the text lives here, fully on black. */}
          {/* One even vertical rhythm: image→name and name→count are both
              16px (pt-4 / gap-4). No fixed height — marathon names
              (Latin+Arabic combos) grow the strip instead of clipping. */}
          {/* Text + actions anchor to the card's LEFT edge (consistent regardless
              of how wide the name renders) with a small inset as a breather;
              the separator/icon row left-aligns on the same edge. */}
          <div className="flex flex-col px-4">
          <div className="flex flex-col items-start gap-4 pt-4 pb-2 text-left">
            <h1
              tabIndex={-1}
              className="font-display text-display font-semibold tracking-tight text-balance"
            >
              {displayName}
            </h1>
            <p
              className="text-muted-foreground min-h-5 text-sm tabular-nums"
              aria-live="polite"
            >
              {!isPending && !isError && total > 0 ? (
                <span ref={countRef} tabIndex={-1}>
                  {total.toLocaleString()} {total === 1 ? "album" : "albums"}
                </span>
              ) : undefined}
            </p>
          </div>
          {/* Per-artist maintenance actions — the Koito idiom: one centered
              row of large icon actions, each named by tooltip + aria-label.
              Status/progress lives in the activity popover; Reorganize's
              transient preview/confirm UI expands below the row. */}
          <div className="mt-2">
            <ReorganizeControl
              scope={{ scope: "artist", artist: displayName }}
              variant="rail"
              railActions={
                <>
                  {imagesEnabled && (
                    <IconAction
                      ref={imageToggleRef}
                      label="Edit artist image"
                      onClick={() => setEditingImage((v) => !v)}
                      aria-expanded={editingImage}
                      aria-controls="artist-image-panel"
                    >
                      <Cover weight="thin" className="size-10" aria-hidden="true" />
                    </IconAction>
                  )}
                  {writeEnabled && (
                    <ArtistArtStatus displayName={displayName} />
                  )}
                  <DeleteArtistAction name={artist} albumCount={total} />
                </>
              }
            />
          </div>
          </div>
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col gap-6">
          <SectionLabel>Albums</SectionLabel>
          {editingImage && (
            <div
              id="artist-image-panel"
              ref={imagePanelRef}
              tabIndex={-1}
              className="outline-none"
            >
              <ArtistImageEditPanel
                name={displayName}
                onSaved={() => setImageVersion((v) => v + 1)}
                onClose={closeImagePanel}
              />
            </div>
          )}

      {isPending ? (
        <PageSkeleton announce="Loading albums…">
          <AlbumsGridSkeleton count={Math.min(PAGE_SIZE, 18)} />
        </PageSkeleton>
      ) : isError ? (
        <ErrorState
          message="Couldn’t load albums. Check the backend and try again."
          onRetry={() => void refetch()}
        />
      ) : data.items.length === 0 ? (
        // total === 0: the artist genuinely has no albums. total > 0 with an
        // empty page means the offset is past the end (stale/hand-crafted
        // URL) — the artist DOES have albums, so don't claim otherwise.
        total === 0 ? (
          <EmptyState
            bordered
            icon={Albums}
            title={`No albums for ${displayName}`}
            body="Nothing in your library is filed under this artist."
            action={<BackLink to="/artists" label="Artists" />}
          />
        ) : (
          <EmptyState
            bordered
            icon={Albums}
            title="Nothing on this page"
            body="This page is past the end of the list."
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={() => goToOffset(0)}
              >
                Back to first page
              </Button>
            }
          />
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
            {/* No `from` origin on purpose: the album page's default back
                link already walks UP the spine to this artist. */}
            {data.items.map((album) => (
              <li key={album.id}>
                <AlbumCard album={album} />
              </li>
            ))}
          </ul>

          {total > PAGE_SIZE && (
            <Pagination
              total={total}
              offset={offset}
              limit={PAGE_SIZE}
              busy={isFetching}
              onOffsetChange={goToOffset}
            />
          )}
        </>
      )}
        </div>
      </div>
    </PageBody>
  );
}

/** The per-artist "Save art to library" action (writes artist-poster /
 * artist-background files into the artist folder for Plex). Just the icon —
 * progress + the failed state show in the topbar activity popover. While any
 * artist-art job runs the button stays FOCUSABLE (aria-disabled + swallowed
 * re-clicks, the Pagination rule — `disabled` on activation would strand
 * keyboard focus on <body> for the whole job); the aria-label stays constant
 * across the spinner swap so the accessible name never flickers. */
function ArtistArtStatus({ displayName }: { displayName: string }) {
  const status = useArtistArtBackfillStatus();
  const start = useStartArtistArtApply(displayName);
  const running = status.data?.phase === "running";
  const busy = start.isPending || running;

  return (
    <IconAction
      label="Save art to library"
      aria-disabled={busy || undefined}
      className="aria-disabled:opacity-50"
      onClick={() => {
        if (busy) return; // job in flight — keep focus, swallow the re-click
        start.mutate();
      }}
    >
      {start.isPending ? (
        <Spinner weight="thin" className="size-10 animate-spin" aria-hidden="true" />
      ) : (
        <SaveArt weight="thin" className="size-10" aria-hidden="true" />
      )}
    </IconAction>
  );
}

