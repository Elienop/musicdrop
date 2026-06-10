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
import { Albums, Cover, Spinner } from "@/components/icons";
import { ReorganizeControl } from "@/components/reorganize/ReorganizeControl";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { PAGE_SIZE, Pagination } from "@/components/system/Pagination";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

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
  // inline panel moves focus into it; closing leaves focus on the toggle.
  const imagePanelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (editingImage) imagePanelRef.current?.focus();
  }, [editingImage]);
  const imageSettings = useArtistImageSettings();
  // The single write-to-library toggle drives BOTH fetch + write; when on, the
  // header gains a per-artist "Apply to library" action with marching progress.
  const artSettings = useArtistArtSettings();
  const imagesEnabled = imageSettings.data?.enabled ?? false;
  const writeEnabled = artSettings.data?.enabled ?? false;
  // Backdrop short-circuit mirrors ArtistImage's rule: only skip the request
  // when BOTH toggles are loaded-and-off (no pointless 404 round-trip); while
  // either loads or is on, attempt it — a failure degrades to the gradient
  // inside ArtistHeroBackdrop.
  const backdropDisabled =
    imageSettings.data?.enabled === false &&
    artSettings.data?.enabled === false;

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
      {/* Art-forward hero (spec §5/§7): blurred-portrait backdrop behind the
          existing poster + name + action row. Layer 1 is decorative
          (aria-hidden); layer 2 is the pre-hero header content unchanged —
          same h1, same buttons, same disclosure wiring. The BackLink above
          and everything below (panel, grid) stay outside the container. */}
      <div className="relative overflow-hidden rounded-xl">
        <ArtistHeroBackdrop
          name={displayName}
          version={imageVersion}
          disabled={backdropDisabled}
        />
        <div className="relative flex flex-col gap-6 p-6 sm:flex-row sm:items-stretch">
          <ArtistImage
            name={displayName}
            decorative
            version={imageVersion}
            className="size-40 shrink-0 rounded-xl shadow-lg"
            monogramClassName="text-6xl"
          />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <PageHeader
              title={displayName}
              meta={
                !isPending && !isError && total > 0 ? (
                  <span ref={countRef} tabIndex={-1}>
                    {total.toLocaleString()} {total === 1 ? "album" : "albums"}
                  </span>
                ) : undefined
              }
            />
            {/* Per-artist maintenance actions — a single button row pushed to
                the bottom of the column so it lines up with the bottom of the
                poster. Status/progress lives in the activity popover.
                border-border is already white/10 in the dark theme, so the
                separator stays legible on the tinted backdrop. */}
            <div className="border-border mt-auto flex flex-wrap items-center gap-3 border-t pt-3">
              {imagesEnabled && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setEditingImage((v) => !v)}
                  aria-label="Edit artist image"
                  aria-expanded={editingImage}
                  aria-controls="artist-image-panel"
                >
                  <Cover className="size-4" aria-hidden="true" /> Image
                </Button>
              )}
              {writeEnabled && <ArtistArtStatus displayName={displayName} />}
              <ReorganizeControl
                scope={{ scope: "artist", artist: displayName }}
              />
            </div>
          </div>
        </div>
      </div>

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
            onClose={() => setEditingImage(false)}
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
    </PageBody>
  );
}

/** The per-artist "Write artist art" action. Just the button — progress + the
 * failed state show in the topbar activity popover, so the action row stays a
 * single clean line. Disabled while any artist-art job runs. */
function ArtistArtStatus({ displayName }: { displayName: string }) {
  const status = useArtistArtBackfillStatus();
  const start = useStartArtistArtApply(displayName);
  const running = status.data?.phase === "running";

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => start.mutate()}
      disabled={start.isPending || running}
      aria-label="Write artist art"
    >
      {start.isPending ? (
        <>
          <Spinner className="size-4 animate-spin" aria-hidden="true" />{" "}
          Starting…
        </>
      ) : (
        "Write artist art"
      )}
    </Button>
  );
}

/** Layer 1 of the artist hero: the portrait as a blurred, scrimmed backdrop.
 * Page-owned, NOT ArtistImage — that component's failed-state renders the
 * initials monogram, which would be wrong as a backdrop. A plain <img> on
 * the same versioned endpoint as the poster, with its own onError state:
 * no portrait (404/decode failure) or feature loaded-and-off → the static
 * violet-tinted gradient. Entirely aria-hidden; the h1 in layer 2 names
 * the artist. */
function ArtistHeroBackdrop({
  name,
  version,
  disabled,
}: {
  name: string;
  version: number;
  disabled: boolean;
}) {
  const [failed, setFailed] = useState(false);
  // A new version (override save) or artist may have a portrait now — retry.
  useEffect(() => setFailed(false), [name, version]);

  if (disabled || failed) {
    return (
      <div
        aria-hidden="true"
        className="from-primary/15 to-surface-base absolute inset-0 bg-gradient-to-br"
      />
    );
  }
  return (
    <div aria-hidden="true" className="absolute inset-0">
      <img
        src={`/api/artists/image?name=${encodeURIComponent(name)}&v=${version}`}
        alt=""
        onError={() => setFailed(true)}
        className="size-full scale-110 object-cover opacity-40 blur-2xl"
      />
      <div className="via-surface-base/70 to-surface-base absolute inset-0 bg-gradient-to-b from-transparent" />
    </div>
  );
}
