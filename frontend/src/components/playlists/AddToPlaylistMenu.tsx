import { AlertCircle, Check, ListPlus, Plus } from "lucide-react";
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
  // add mutation resolves — so feedback can't live inside the menu. The result
  // is conveyed two ways on the always-mounted root: an always-present
  // role="status" region announces it to assistive tech (a live region must
  // exist BEFORE it is populated to be announced reliably), and the trigger
  // icon briefly swaps to a check / alert so sighted users get an in-flow cue
  // that the surrounding (overflow-clipped) Table can't hide.
  const [feedback, setFeedback] = useState<"idle" | "added" | "error">("idle");
  const [addedName, setAddedName] = useState("");
  const addTracks = useAddTracks();

  // Self-dismiss the transient cue so it doesn't linger on later interactions.
  useEffect(() => {
    if (feedback === "idle") {
      return;
    }
    const timer = setTimeout(() => setFeedback("idle"), 3000);
    return () => clearTimeout(timer);
  }, [feedback]);

  function addTo(playlistId: string, name: string) {
    addTracks.mutate(
      { playlistId, trackIds },
      {
        onSuccess: () => {
          setAddedName(name);
          setFeedback("added");
        },
        onError: () => setFeedback("error"),
      },
    );
  }

  const statusText =
    feedback === "added"
      ? `Added to ${addedName}`
      : feedback === "error"
        ? "Couldn't add — try again"
        : "";
  const TriggerIcon =
    feedback === "added" ? Check : feedback === "error" ? AlertCircle : ListPlus;
  const triggerIconClass =
    feedback === "added"
      ? "size-4 text-success"
      : feedback === "error"
        ? "size-4 text-destructive"
        : "size-4";

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon-sm" aria-label={menuLabel}>
            <TriggerIcon className={triggerIconClass} aria-hidden="true" />
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

      {/* Always-mounted status region so the result is reliably announced.
          role="status" already implies a polite live region — we omit the
          explicit aria-live so this per-row menu doesn't multiply the page's
          count of explicit aria-live regions (e.g. SearchPage's announcer). */}
      <span role="status" className="sr-only">
        {statusText}
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
