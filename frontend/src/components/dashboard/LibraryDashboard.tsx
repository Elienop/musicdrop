// frontend/src/components/dashboard/LibraryDashboard.tsx
import { SectionLabel } from "@/components/system/SectionLabel";
import { Link } from "react-router";

import type { LibraryStatsResponse } from "@/api/useStats";
import { useStats } from "@/api/useStats";
import { useActiveImport } from "@/api/useActiveImport";
import { useActivity } from "@/api/useActivity";
import { AlbumCard, GRID_CLASS } from "@/components/albums/album-grid";
import {
  Albums,
  Artists,
  Duration,
  MusicFallback,
  Review,
  Storage,
  Track,
} from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { JobProgress } from "@/components/system/JobProgress";
import { StatusBanner } from "@/components/system/StatusBanner";
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
const TILE_GRID =
  "grid grid-cols-2 gap-x-6 gap-y-8 py-6 sm:grid-cols-3 lg:grid-cols-5";

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
            {/* h-14 = the cardless StatTile row height (its documented
              skeleton contract) — no shift when stats land. */}
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-14 rounded-lg" />
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
    <>
      {/* PageBody's gap-6 column now spaces these siblings (the old wrapper
          section's internal gap-6, one level up). */}
      <section aria-label="Library stats" className={TILE_GRID}>
        {tiles.map((tile) => (
          <StatTile
            key={tile.label}
            icon={tile.icon}
            label={tile.label}
            value={tile.value}
          />
        ))}
      </section>

      <AcquisitionGlance />
      <ReviewPendingBanner />

      {recently_added.length > 0 ? (
        <div className="flex flex-col gap-3">
          {/* One section-heading dialect app-wide: SectionLabel. */}
          <SectionLabel>Recently added</SectionLabel>
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
    </>
  );
}

/** Phase-4 Overview glance (spec §7): a compact acquisition/jobs strip
 * between the stat tiles and "Recently added", rendered ONLY while
 * something is running or failed — an idle dashboard gets zero dead
 * chrome. Reuses the activity read model + JobProgress verbatim and caps
 * at three rows; the topbar popover stays the full surface. */
function AcquisitionGlance() {
  const { rows, runningCount } = useActivity();
  const hasFailed = rows.some((row) => row.state === "failed");
  if (runningCount === 0 && !hasFailed) {
    return null;
  }
  // Import/acquisition activity means decisions may be queueing on the
  // Review page; pure maintenance jobs (lyrics/art/reorganize) don't.
  const hasReviewKind = rows.some(
    (row) => row.kind === "import" || row.kind === "acquisition",
  );
  return (
    <section
      aria-labelledby="acquisition-glance"
      className="rounded-xl border p-4"
    >
      <div className="flex items-center justify-between gap-2">
        <SectionLabel id="acquisition-glance">Acquisition</SectionLabel>
        {hasReviewKind && (
          <Button variant="ghost" size="sm" asChild>
            <Link to="/review">View Review</Link>
          </Button>
        )}
      </div>
      {/* JobProgress rows carry their own px-4 — pull them back to the
          section edge so their inset matches the p-4 frame. Failed rows sort
          first so the 3-row cap can never hide the very thing the glance
          exists to surface (useActivity's source order is fixed and a late
          failed row would otherwise be cut when 4-5 rows coexist). */}
      <ul className="divide-border -mx-4 divide-y">
        {[
          ...rows.filter((row) => row.state === "failed"),
          ...rows.filter((row) => row.state !== "failed"),
        ]
          .slice(0, 3)
          .map((row) => (
            <li key={row.id}>
              <JobProgress
                label={row.label}
                scope={row.scope}
                state={row.state}
                progress={row.progress}
                counts={row.countsText}
                href={row.href}
              />
            </li>
          ))}
      </ul>
    </section>
  );
}

/** One-line pointer to pending import decisions — the durable counterpart
 * to the transient glance above (set-aside items outlive the running job). */
function ReviewPendingBanner() {
  const needsReview = useActiveImport().data?.needs_review_count ?? 0;
  if (needsReview === 0) {
    return null;
  }
  return (
    <StatusBanner
      tone="neutral"
      icon={Review}
      action={
        <Button variant="ghost" size="sm" asChild>
          <Link to="/review">Review</Link>
        </Button>
      }
    >
      {needsReview.toLocaleString()}{" "}
      {needsReview === 1 ? "decision" : "decisions"} awaiting review.
    </StatusBanner>
  );
}
