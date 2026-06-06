// frontend/src/components/dashboard/LibraryDashboard.tsx
import {
  AlertCircle,
  Clock,
  Disc3,
  HardDrive,
  Music,
  Users,
} from "lucide-react";

import { useStats } from "@/api/useStats";
import { AlbumCard, GRID_CLASS } from "@/components/albums/album-grid";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatBytes, formatTotalDuration } from "@/lib/format";

export function LibraryDashboard() {
  const { data, isPending, isError } = useStats();

  if (isPending) {
    return (
      <section aria-label="Library stats" className="flex flex-col gap-4">
        <p className="sr-only" role="status">
          Loading library stats&hellip;
        </p>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
          {Array.from({ length: 5 }, (_, i) => (
            <Skeleton key={i} className="h-24 rounded-xl" />
          ))}
        </div>
      </section>
    );
  }

  if (isError || !data) {
    return (
      <section aria-label="Library stats">
        <div
          className="border-destructive/40 bg-destructive/5 flex items-center gap-3 rounded-xl border p-3 text-sm"
          role="alert"
        >
          <AlertCircle
            className="text-destructive size-5 shrink-0"
            aria-hidden="true"
          />
          <span>Could not load library stats.</span>
        </div>
      </section>
    );
  }

  const { stats, recently_added, size_is_estimate } = data;
  const cards = [
    { icon: Music, label: "Tracks", value: stats.track_count.toLocaleString() },
    { icon: Disc3, label: "Albums", value: stats.album_count.toLocaleString() },
    {
      icon: Users,
      label: "Artists",
      value: stats.artist_count.toLocaleString(),
    },
    {
      icon: Clock,
      label: "Duration",
      value: formatTotalDuration(stats.total_seconds),
    },
    {
      icon: HardDrive,
      label: "Size",
      value: `${size_is_estimate ? "~" : ""}${formatBytes(stats.total_bytes)}`,
    },
  ];

  return (
    <section aria-label="Library stats" className="flex flex-col gap-6">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
        {cards.map(({ icon: Icon, label, value }) => (
          <Card key={label}>
            <CardContent className="flex flex-col gap-1 p-4">
              <span className="text-muted-foreground flex items-center gap-1.5 text-sm">
                <Icon className="size-4" aria-hidden="true" />
                {label}
              </span>
              <span className="text-2xl font-semibold tracking-tight tabular-nums">
                {value}
              </span>
            </CardContent>
          </Card>
        ))}
      </div>

      {recently_added.length > 0 ? (
        <div className="flex flex-col gap-3">
          <h2 className="text-2xl font-semibold tracking-tight">
            Recently added
          </h2>
          <ul className={GRID_CLASS}>
            {recently_added.map((album) => (
              <li key={album.id}>
                <AlbumCard album={album} />
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-muted-foreground text-sm">
          Import some music to get started.
        </p>
      )}
    </section>
  );
}
