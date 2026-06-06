import { ListPlus, Plus } from "lucide-react";
import { useEffect, useState } from "react";

import { useAddTracks, usePlaylists } from "@/api/usePlaylists";
import { CreatePlaylistDialog } from "@/components/playlists/CreatePlaylistDialog";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * Self-contained "Add to playlist" affordance: a ghost icon button that opens a
 * dropdown of existing playlists (plus "New playlist…"). Drops into any track
 * row — pass the beets item id(s) it should add. The playlist list is only
 * fetched when the menu opens (the query lives inside the dropdown content,
 * which radix mounts on open), so a row carrying this menu costs no request
 * until used.
 */
export function AddToPlaylistMenu({
  trackIds,
  label,
}: {
  trackIds: number[];
  label?: string;
}) {
  const menuLabel = label ?? "Add to playlist";
  const [createOpen, setCreateOpen] = useState(false);
  // Radix closes the dropdown on item-select, unmounting its content before the
  // add mutation resolves — so any feedback rendered inside the menu would be
  // torn down before it could speak. The confirmation lives on the always-mounted
  // root instead: role="status" announces it to assistive tech and it is visible
  // (a small floating pill) so sighted users also get acknowledgement.
  const [msg, setMsg] = useState("");
  const addTracks = useAddTracks();

  // Self-dismiss so the floating confirmation doesn't linger over later rows.
  useEffect(() => {
    if (!msg) {
      return;
    }
    const timer = setTimeout(() => setMsg(""), 3000);
    return () => clearTimeout(timer);
  }, [msg]);

  function addTo(playlistId: string, name: string) {
    addTracks.mutate(
      { playlistId, trackIds },
      {
        onSuccess: () => setMsg(`Added to ${name}`),
        onError: () => setMsg("Couldn't add — try again"),
      },
    );
  }

  return (
    <>
      <span className="relative inline-flex">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label={menuLabel}>
              <ListPlus className="size-4" aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-52">
            <DropdownMenuLabel>Add to playlist</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <PlaylistItems onPick={addTo} pending={addTracks.isPending} />
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={(e) => {
                // Keep the dropdown's select from also dismissing the dialog we open.
                e.preventDefault();
                setCreateOpen(true);
              }}
            >
              <Plus className="size-4" aria-hidden="true" /> New playlist&hellip;
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        {msg && (
          <span
            role="status"
            aria-live="polite"
            className="bg-foreground text-background pointer-events-none absolute top-full right-0 z-50 mt-1 rounded-md px-2 py-1 text-xs whitespace-nowrap shadow-md"
          >
            {msg}
          </span>
        )}
      </span>

      <CreatePlaylistDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={(playlist) => addTo(playlist.id, playlist.name)}
      />
    </>
  );
}

/** The existing-playlist rows. `usePlaylists` lives here (inside the dropdown
 * content) so it only fetches once the menu is open. */
function PlaylistItems({
  onPick,
  pending,
}: {
  onPick: (playlistId: string, name: string) => void;
  pending: boolean;
}) {
  const { data, isPending, isError } = usePlaylists();

  if (isPending) {
    return (
      <DropdownMenuItem disabled>Loading playlists&hellip;</DropdownMenuItem>
    );
  }
  if (isError) {
    return <DropdownMenuItem disabled>Couldn&rsquo;t load playlists</DropdownMenuItem>;
  }
  if (data.length === 0) {
    return <DropdownMenuItem disabled>No playlists yet</DropdownMenuItem>;
  }
  return (
    <>
      {data.map((playlist) => (
        <DropdownMenuItem
          key={playlist.id}
          disabled={pending}
          onSelect={() => onPick(playlist.id, playlist.name)}
        >
          <span className="truncate">{playlist.name}</span>
        </DropdownMenuItem>
      ))}
    </>
  );
}
