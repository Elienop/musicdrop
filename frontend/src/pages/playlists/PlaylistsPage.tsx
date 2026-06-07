import { ListMusic, Plus } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { type Playlist, usePlaylists } from "@/api/usePlaylists";
import { CreatePlaylistDialog } from "@/components/playlists/CreatePlaylistDialog";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export function PlaylistsPage() {
  const navigate = useNavigate();
  const { data, isPending, isError, refetch } = usePlaylists();
  const [createOpen, setCreateOpen] = useState(false);

  return (
    <section className="flex flex-col gap-6" aria-label="Playlists">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2 className="text-2xl font-semibold tracking-tight">Playlists</h2>
          <p className="text-muted-foreground text-sm">
            {data
              ? `${data.length} ${data.length === 1 ? "playlist" : "playlists"}`
              : "Loading your playlists…"}
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="size-4" aria-hidden="true" /> New playlist
        </Button>
      </header>

      {isPending && <ListSkeleton />}
      {isError && <ErrorState onRetry={() => void refetch()} />}
      {data && data.length === 0 && <EmptyState />}
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

/** A single playlist row: whole-card link to its editor (name + track count). */
function PlaylistRow({ playlist }: { playlist: Playlist }) {
  return (
    <Link
      to={`/playlists/${playlist.id}`}
      className="focus-visible:ring-ring block rounded-xl focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none"
    >
      <Card className="hover:border-primary/50 gap-2 py-4 transition-colors">
        <CardHeader className="px-4">
          <CardTitle className="truncate" title={playlist.name}>
            {playlist.name}
          </CardTitle>
          <CardDescription>
            {playlist.track_count} {playlist.track_count === 1 ? "track" : "tracks"}
          </CardDescription>
        </CardHeader>
      </Card>
    </Link>
  );
}

function ListSkeleton() {
  return (
    <ul className="flex flex-col gap-3" aria-hidden="true">
      {Array.from({ length: 4 }, (_, i) => (
        <li key={i}>
          <Card className="gap-2 py-4">
            <CardHeader className="gap-2 px-4">
              <Skeleton className="h-5 w-48" />
              <Skeleton className="h-4 w-20" />
            </CardHeader>
          </Card>
        </li>
      ))}
    </ul>
  );
}

function EmptyState() {
  return (
    <div className="rounded-xl border border-dashed p-8 text-center" role="status">
      <ListMusic className="text-muted-foreground mx-auto mb-2 size-8" aria-hidden="true" />
      <p className="font-medium">No playlists yet</p>
      <p className="text-muted-foreground text-sm">
        Create one, then add tracks from any album or from search.
      </p>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      className="border-destructive/40 bg-destructive/5 flex items-center justify-between gap-3 rounded-xl border p-4"
      role="alert"
    >
      <p className="text-sm">Couldn&rsquo;t load playlists.</p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
