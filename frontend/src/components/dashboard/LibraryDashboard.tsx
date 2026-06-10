// frontend/src/components/dashboard/LibraryDashboard.tsx
import { Link } from "react-router";

import type { LibraryStatsResponse } from "@/api/useStats";
import { useStats } from "@/api/useStats";
import { AlbumCard, GRID_CLASS } from "@/components/albums/album-grid";
import {
  Albums,
  Artists,
  Duration,
  MusicFallback,
  Storage,
  Track,
} from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { StatTile } from "@/components/system/StatTile";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { formatBytes, formatTotalDuration } from "@/lib/format";

/** Origin recorded on every album card opened from the Overview (spec §1
 * origin threading): the album page's back link returns here. */
const OVERVIEW_ORIGIN = { label: "Overview", to: "/" } as const;

/** Tile grid shared by the loaded StatTiles and their skeleton bones. */
const TILE_GRID = "grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5";

export function LibraryDashboard() {
  const { data, isPending, isError, refetch } = useStats();

  return (
    <PageBody>
      <PageHeader
        title="Overview"
        meta={
          data !== undefined
            ? `${data.stats.album_count.toLocaleString()} albums · ${data.stats.track_count.toLocaleString()} tracks`
            : undefined
        }
      />
      {isPending ? (
        <PageSkeleton announce="Loading library stats…">
          <div className={TILE_GRID}>
            {/* h-22 = the hint-less StatTile's fixed content height (its
                documented skeleton contract) — no shift when tiles land. */}
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-22 rounded-xl" />
            ))}
          </div>
        </PageSkeleton>
      ) : isError ? (
        <ErrorState
          message="Could not load library stats."
          onRetry={() => void refetch()}
        />
      ) : (
        <DashboardBody data={data} />
      )}
    </PageBody>
  );
}

function DashboardBody({ data }: { data: LibraryStatsResponse }) {
  const { stats, recently_added, size_is_estimate } = data;
  const tiles = [
    { icon: Track, label: "Tracks", value: stats.track_count.toLocaleString() },
    { icon: Albums, label: "Albums", value: stats.album_count.toLocaleString() },
    {
      icon: Artists,
      label: "Artists",
      value: stats.artist_count.toLocaleString(),
    },
    {
      icon: Duration,
      label: "Duration",
      value: formatTotalDuration(stats.total_seconds),
    },
    {
      icon: Storage,
      label: "Size",
      value: `${size_is_estimate ? "~" : ""}${formatBytes(stats.total_bytes)}`,
    },
  ];

  return (
    <section aria-label="Library stats" className="flex flex-col gap-6">
      <div className={TILE_GRID}>
        {tiles.map((tile) => (
          <StatTile
            key={tile.label}
            icon={tile.icon}
            label={tile.label}
            value={tile.value}
          />
        ))}
      </div>

      {recently_added.length > 0 ? (
        <div className="flex flex-col gap-3">
          {/* Section scale (spec §3): h2 text-base font-semibold under the h1. */}
          <h2 className="text-base font-semibold">Recently added</h2>
          <ul className={GRID_CLASS}>
            {recently_added.map((album) => (
              <li key={album.id}>
                <AlbumCard album={album} from={OVERVIEW_ORIGIN} />
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <EmptyState
          bordered
          icon={MusicFallback}
          title="Your library is empty"
          body="Import some music to get started."
          action={
            <Button variant="outline" size="sm" asChild>
              <Link to="/import">Add music from a folder</Link>
            </Button>
          }
        />
      )}
    </section>
  );
}
