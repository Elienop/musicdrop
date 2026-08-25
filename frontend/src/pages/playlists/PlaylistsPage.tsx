import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { type Playlist, usePlaylists } from "@/api/usePlaylists";
import { Add, Playlists, Upload } from "@/components/icons";
import { CreatePlaylistDialog } from "@/components/playlists/CreatePlaylistDialog";
import { PlaylistCover } from "@/components/playlists/PlaylistCover";
import { StatusLine, playlistSyncStatus } from "@/components/playlists/plexSyncStatus";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageHeader } from "@/components/system/PageHeader";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { plural } from "@/lib/format";

export function PlaylistsPage() {
  const navigate = useNavigate();
  const { data, isPending, isError, refetch } = usePlaylists();
  const [createOpen, setCreateOpen] = useState(false);

  return (
    <section className="flex flex-col gap-6" aria-label="Playlists">
      <PageHeader
        title="Playlists"
        meta={
          data
            ? `${data.length} ${plural(data.length, "playlist")}`
            : undefined
        }
        actions={
          <>
            <Button variant="outline" asChild>
              <Link to="/playlists/import">
                <Upload className="size-4" aria-hidden="true" /> Import
              </Link>
            </Button>
            <Button onClick={() => setCreateOpen(true)}>
              <Add className="size-4" aria-hidden="true" /> New playlist
            </Button>
          </>
        }
      />

      {isPending && (
        <PageSkeleton announce="Loading playlists…">
          <ListSkeleton />
        </PageSkeleton>
      )}
      {isError && (
        <ErrorState
          variant="inline"
          message="Couldn’t load playlists."
          onRetry={() => void refetch()}
        />
      )}
      {data && data.length === 0 && (
        <EmptyState
          icon={Playlists}
          title="No playlists yet"
          body="Create one, then add tracks from any album or from search."
          bordered
        />
      )}
      {data && data.length > 0 && (
        <ul className="flex flex-col gap-3">
          {data.map((playlist) => (
            <li key={playlist.id}>
              <PlaylistRow playlist={playlist} />
            </li>
          ))}
        </ul>
      )}

      <CreatePlaylistDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={(playlist) => navigate(`/playlists/${playlist.id}`)}
      />
    </section>
  );
}

/** A single playlist row: whole-card link to its editor (name + track count).
 * A Plex sync badge sits on the right; the name area is the flexible,
 * truncating middle (min-w-0 + flex-1) so a long name never pushes the status
 * out of view. A playlist with no Plex state at all shows no badge — a muted
 * "Not synced" on every quiet row would be noise (see playlistSyncStatus). */
function PlaylistRow({ playlist }: Readonly<{ playlist: Playlist }>) {
  const status = playlistSyncStatus(playlist);
  return (
    <Link
      to={`/playlists/${playlist.id}`}
      className="focus-ring block rounded-xl"
    >
      <Card className="hover:bg-surface-hover gap-2 py-4 transition-colors">
        <CardHeader className="flex flex-row items-center gap-3 px-4">
          <PlaylistCover
            playlist={playlist}
            className="border-border size-14 shrink-0 rounded-lg border"
          />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <CardTitle className="truncate" title={playlist.name}>
              {playlist.name}
            </CardTitle>
            <CardDescription>
              {playlist.track_count} {playlist.track_count === 1 ? "track" : "tracks"}
            </CardDescription>
          </div>
          {status ? (
            <span className="shrink-0">
              <StatusLine status={status} />
            </span>
          ) : null}
        </CardHeader>
      </Card>
    </Link>
  );
}

/** Bones only — PageSkeleton owns the aria-hidden + the loading announcement. */
function ListSkeleton() {
  return (
    <ul className="flex flex-col gap-3">
      {Array.from({ length: 4 }, (_, i) => (
        <li key={i}>
          <Card className="gap-2 py-4">
            <CardHeader className="flex flex-row items-center gap-3 px-4">
              <Skeleton className="size-14 shrink-0 rounded-lg" />
              <div className="flex flex-col gap-2">
                <Skeleton className="h-5 w-48" />
                <Skeleton className="h-4 w-20" />
              </div>
            </CardHeader>
          </Card>
        </li>
      ))}
    </ul>
  );
}
