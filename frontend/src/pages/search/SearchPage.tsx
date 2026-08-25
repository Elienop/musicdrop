import { SectionLabel } from "@/components/system/SectionLabel";
import { useRef } from "react";
import { Link, useSearchParams } from "react-router";

import type { SearchTrack, SearchType, TypedSearchPage } from "@/api/useSearch";
import { parseSearchType, useSearch, useTypedSearch } from "@/api/useSearch";
import {
  AlbumCard,
  type AlbumOrigin,
  AlbumsGridSkeleton,
  BackLink,
  GRID_CLASS,
} from "@/components/albums/album-grid";
import { ArtistCard } from "@/components/artists/ArtistCard";
import { Search } from "@/components/icons";
import { AddToPlaylistMenu } from "@/components/playlists/AddToPlaylistMenu";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { PAGE_SIZE, Pagination } from "@/components/system/Pagination";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * /search — sectioned overview by default; `?type=artists|albums|tracks`
 * switches to a paged single-type view of the same query ("View all N").
 * Both views are URL-state (q/type/offset), so results are bookmarkable and
 * Back works. Each view owns its query hook, so the dispatcher keeps hook
 * order stable.
 */
export function SearchPage() {
  const [searchParams] = useSearchParams();
  const q = (searchParams.get("q") ?? "").trim();
  const type = parseSearchType(searchParams.get("type"));

  if (type !== null && q.length > 0) {
    return <TypedSearchView q={q} type={type} />;
  }
  return <SectionedSearchView q={q} />;
}

function SectionedSearchView({ q }: Readonly<{ q: string }>) {
  const [searchParams] = useSearchParams();
  const { data, isPending, isError, isFetching, isPlaceholderData, refetch } =
    useSearch(q);

  // Idle: blank query — prompt rather than "no results" (no request was made).
  if (q.length === 0) {
    return (
      <PageBody>
        <PageHeader title="Search" />
        <EmptyState
          icon={Search}
          title="Search your library"
          body="Find artists, albums, and tracks by name."
        />
      </PageBody>
    );
  }

  if (isPending) {
    return (
      <PageBody>
        <PageHeader title={`Results for “${q}”`} />
        <PageSkeleton announce="Searching…">
          <SearchSkeleton />
        </PageSkeleton>
      </PageBody>
    );
  }

  if (isError) {
    return (
      <PageBody>
        <PageHeader title={`Results for “${q}”`} />
        <ErrorState
          message="Couldn’t run the search. Check the backend and try again."
          onRetry={() => void refetch()}
        />
      </PageBody>
    );
  }

  // Derive "empty" from what we'd actually RENDER (the arrays), not the server
  // totals — so a total/array divergence can never leave a blank results area.
  const shownCount =
    data.artists.length + data.albums.length + data.tracks.length;

  if (shownCount === 0) {
    return (
      <PageBody>
        <PageHeader title={`Results for “${q}”`} meta="0 results" />
        {/* Future (slskd project): the "Search Soulseek for “{q}”" escape
            renders in this EmptyState's `action` slot when the Find music
            page ships (spec §0 search model). */}
        <EmptyState
          icon={Search}
          title={`No results for “${q}”`}
          body="Try a different term."
        />
      </PageBody>
    );
  }

  // In-flight cue while refining an existing query (placeholderData keeps the
  // previous results visible): dim + aria-busy, matching the album grid.
  const refining = isFetching && isPlaceholderData;
  // Origin recorded on every album/track link out of this view (spec §1).
  const from: AlbumOrigin = {
    label: "Search",
    to: `/search?${searchParams.toString()}`,
  };
  const viewAllTo = (sectionType: SearchType): string => {
    const next = new URLSearchParams(searchParams);
    next.set("type", sectionType);
    next.delete("offset");
    return `/search?${next.toString()}`;
  };

  return (
    <PageBody>
      <PageHeader
        title={`Results for “${q}”`}
        meta={`${shownCount} ${shownCount === 1 ? "result" : "results"}`}
      />
      <div
        className={cn(
          "flex flex-col gap-10",
          refining && "pointer-events-none opacity-60 transition-opacity",
        )}
        aria-busy={refining}
      >
        {data.artists.length > 0 && (
          <ResultSection
            title="Artists"
            shown={data.artists.length}
            total={data.artist_total}
            viewAllTo={
              data.artist_total > data.artists.length
                ? viewAllTo("artists")
                : undefined
            }
          >
            <ul className={GRID_CLASS}>
              {data.artists.map((artist) => (
                <li key={artist.name}>
                  <ArtistCard artist={artist} />
                </li>
              ))}
            </ul>
          </ResultSection>
        )}

        {data.albums.length > 0 && (
          <ResultSection
            title="Albums"
            shown={data.albums.length}
            total={data.album_total}
            viewAllTo={
              data.album_total > data.albums.length
                ? viewAllTo("albums")
                : undefined
            }
          >
            <ul className={GRID_CLASS}>
              {data.albums.map((album) => (
                <li key={album.id}>
                  <AlbumCard album={album} from={from} />
                </li>
              ))}
            </ul>
          </ResultSection>
        )}

        {data.tracks.length > 0 && (
          <ResultSection
            title="Tracks"
            shown={data.tracks.length}
            total={data.track_total}
            viewAllTo={
              data.track_total > data.tracks.length
                ? viewAllTo("tracks")
                : undefined
            }
          >
            {/* A flat list of self-contained rows — the playlist seam. */}
            <ul className="border-border divide-border divide-y rounded-xl border">
              {data.tracks.map((track) => (
                <li key={track.id}>
                  <TrackRow track={track} from={from} />
                </li>
              ))}
            </ul>
          </ResultSection>
        )}
      </div>
    </PageBody>
  );
}

/** Human noun for the typed count line. */
const TYPE_NOUN: Record<SearchType, string> = {
  artists: "artists",
  albums: "albums",
  tracks: "tracks",
};

/** How many items of the requested type THIS page carries. */
function shownCountFor(type: SearchType, data: TypedSearchPage): number {
  if (type === "artists") {
    return data.artists.length;
  }
  if (type === "albums") {
    return data.albums.length;
  }
  return data.tracks.length;
}

/** The paged result list for one type — grid for artists/albums, a flat row
 * list for tracks. */
function TypedResultsList({
  type,
  data,
  from,
}: Readonly<{
  type: SearchType;
  data: TypedSearchPage;
  from: AlbumOrigin;
}> ) {
  if (type === "artists") {
    return (
      <ul className={GRID_CLASS}>
        {data.artists.map((artist) => (
          <li key={artist.name}>
            <ArtistCard artist={artist} />
          </li>
        ))}
      </ul>
    );
  }
  if (type === "albums") {
    return (
      <ul className={GRID_CLASS}>
        {data.albums.map((album) => (
          <li key={album.id}>
            <AlbumCard album={album} from={from} />
          </li>
        ))}
      </ul>
    );
  }
  return (
    <ul className="border-border divide-border divide-y rounded-xl border">
      {data.tracks.map((track) => (
        <li key={track.id}>
          <TrackRow track={track} from={from} />
        </li>
      ))}
    </ul>
  );
}

function TypedSearchView({ q, type }: Readonly<{ q: string; type: SearchType }>) {
  const [searchParams, setSearchParams] = useSearchParams();
  const offset = Math.max(0, Number(searchParams.get("offset") ?? "0") || 0);
  const { data, isPending, isError, isFetching, isPlaceholderData, refetch } =
    useTypedSearch(q, type, PAGE_SIZE, offset);

  // Post-page-change contract (spec §6): plain scroll to top + move focus to
  // the always-mounted count line in the PageHeader meta region.
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

  // "← All results": the same query, back in the sectioned view.
  const allParams = new URLSearchParams(searchParams);
  allParams.delete("type");
  allParams.delete("offset");
  const allResultsTo = `/search?${allParams.toString()}`;
  // Origin includes type+offset so "back" restores THIS page of the view.
  const from: AlbumOrigin = {
    label: "Search",
    to: `/search?${searchParams.toString()}`,
  };
  const refining = isFetching && isPlaceholderData;

  let body: React.ReactNode;
  if (isPending) {
    body = (
      <PageSkeleton announce="Searching…">
        {type === "tracks" ? (
          <TrackListSkeleton />
        ) : (
          <AlbumsGridSkeleton count={12} />
        )}
      </PageSkeleton>
    );
  } else if (isError) {
    body = (
      <ErrorState
        message="Couldn’t run the search. Check the backend and try again."
        onRetry={() => void refetch()}
      />
    );
  } else {
    const total = data.total;
    const shown = shownCountFor(type, data);

    if (total === 0) {
      body = (
        <EmptyState
          icon={Search}
          title={`No results for “${q}”`}
          body="Try a different term."
        />
      );
    } else if (shown === 0) {
      // total > 0 but this page is empty → the offset is past the end.
      body = (
        <EmptyState
          bordered
          icon={Search}
          title="Nothing on this page"
          body="This page is past the end of the results."
          action={
            <Button variant="outline" size="sm" onClick={() => goToOffset(0)}>
              Back to first page
            </Button>
          }
        />
      );
    } else {
      body = (
        <>
          <div
            className={cn(
              refining && "pointer-events-none opacity-60 transition-opacity",
            )}
            aria-busy={refining}
          >
            <TypedResultsList type={type} data={data} from={from} />
          </div>
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
      );
    }
  }

  return (
    <PageBody>
      <BackLink to={allResultsTo} label="All results" />
      <PageHeader
        title={`Results for “${q}”`}
        meta={
          data !== undefined ? (
            <span ref={countRef} tabIndex={-1}>
              {data.total.toLocaleString()} {TYPE_NOUN[type]}
            </span>
          ) : undefined
        }
      />
      {body}
    </PageBody>
  );
}

/** Section wrapper: heading + "{shown} of {total}" + an optional "View all"
 * link into the paged single-type view of the same query. */
function ResultSection({
  title,
  shown,
  total,
  viewAllTo,
  children,
}: Readonly<{
  title: string;
  shown: number;
  total: number;
  viewAllTo?: string;
  children: React.ReactNode;
}> ) {
  return (
    <section className="flex flex-col gap-4" aria-label={title}>
      <div className="flex items-baseline gap-3">
        {/* One section-heading dialect app-wide: SectionLabel. */}
        <SectionLabel>{title}</SectionLabel>
        <span className="text-muted-foreground text-sm tabular-nums">
          {shown.toLocaleString()} of {total.toLocaleString()}
        </span>
        {viewAllTo !== undefined && (
          <Link
            to={viewAllTo}
            className="focus-ring text-primary-light ml-auto rounded-sm text-sm hover:underline"
          >
            {/* The space rides in the SAME text node as the count — a separate
                whitespace-only node is dropped from the accessible name, which
                would glue "87" onto the sr-only "tracks". */}
            {`View all ${total.toLocaleString()} `}
            <span className="sr-only">{title.toLowerCase()}</span>
          </Link>
        )}
      </div>
      {children}
    </section>
  );
}

/**
 * A single track hit. Every row shares identical chrome (padding/layout); only
 * the TITLE is a link (→ its album page, carrying the Search origin in router
 * state) — so the link's accessible name is just the title. Singletons (no
 * `album_id`) render the title as plain text but keep the same row shape.
 */
function TrackRow({ track, from }: Readonly<{ track: SearchTrack; from: AlbumOrigin }>) {
  return (
    <div className="flex min-w-0 items-center gap-3 px-4 py-3">
      <div className="flex min-w-0 flex-1 flex-col">
        {track.album_id === null ? (
          <span className="truncate font-medium">{track.title}</span>
        ) : (
          <Link
            to={`/albums/${track.album_id}`}
            state={{ from }}
            className="focus-ring w-fit max-w-full truncate rounded-sm font-medium hover:underline"
          >
            {track.title}
          </Link>
        )}
        <span className="text-muted-foreground truncate text-sm">
          {track.artist}
          {track.album && (
            <>
              <span aria-hidden="true"> &middot; </span>
              {track.album}
            </>
          )}
        </span>
      </div>
      <span className="text-muted-foreground shrink-0 text-sm tabular-nums">
        {formatDuration(track.duration_seconds)}
      </span>
      <AddToPlaylistMenu
        trackIds={[track.id]}
        label={`Add ${track.title} to playlist`}
      />
    </div>
  );
}

/** Mirrors the results shape (sections of cards + track rows) so the
 * first-load placeholder doesn't shift the layout. PageSkeleton supplies the
 * aria-hidden + status announcement. */
function SearchSkeleton() {
  return (
    <div className="flex flex-col gap-8">
      {/* A card-grid section (stands in for Artists/Albums). */}
      <div className="flex flex-col gap-4">
        <Skeleton className="h-6 w-32" />
        <ul className={GRID_CLASS}>
          {Array.from({ length: 5 }, (_, i) => (
            <li key={i}>
              <Card className="h-full gap-3 overflow-hidden py-0 pb-4">
                <Skeleton className="aspect-square w-full rounded-none" />
                <CardHeader className="gap-2 px-4 pt-3">
                  <Skeleton className="h-5 w-3/4" />
                </CardHeader>
                <CardContent className="px-4">
                  <Skeleton className="h-4 w-16" />
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      </div>
      {/* A tracks-list section. */}
      <div className="flex flex-col gap-4">
        <Skeleton className="h-6 w-28" />
        <TrackListSkeleton />
      </div>
    </div>
  );
}

function TrackListSkeleton() {
  return (
    <div className="border-border divide-border divide-y rounded-xl border">
      {Array.from({ length: 4 }, (_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3">
          <div className="flex flex-1 flex-col gap-2">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-3 w-1/2" />
          </div>
          <Skeleton className="h-4 w-10" />
        </div>
      ))}
    </div>
  );
}
