import { Spinner } from "@/components/icons";
import { useState } from "react";

import { type Playlist, useCreatePlaylist } from "@/api/usePlaylists";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

/**
 * Controlled "New playlist" dialog: a single Name field + Create. Shared by the
 * playlists list page and the add-to-playlist menu so both create the same way.
 * `onCreated` fires with the new playlist after a successful POST (the list page
 * navigates to it; the menu adds the pending tracks to it).
 */
export function CreatePlaylistDialog({
  open,
  onOpenChange,
  onCreated,
}: Readonly<{
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated?: (playlist: Playlist) => void;
}> ) {
  const [name, setName] = useState("");
  const create = useCreatePlaylist();
  const trimmed = name.trim();

  function close(next: boolean) {
    // Reset the field whenever the dialog closes so it reopens clean.
    if (!next) {
      setName("");
    }
    onOpenChange(next);
  }

  function submit(e: React.SubmitEvent) {
    e.preventDefault();
    if (trimmed.length === 0 || create.isPending) {
      return;
    }
    create.mutate(
      { name: trimmed },
      {
        onSuccess: (playlist) => {
          setName("");
          onOpenChange(false);
          onCreated?.(playlist);
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New playlist</DialogTitle>
          <DialogDescription>
            Name your playlist, then add tracks from any album or from search.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            {/* Native label — there is no shadcn Label component. */}
            <label htmlFor="playlist-name" className="text-sm font-medium">
              Name
            </label>
            <Input
              id="playlist-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Late night"
              autoFocus
            />
          </div>
          {create.isError && (
            <p className="text-destructive text-sm" role="alert">
              Couldn&rsquo;t create the playlist. Try again.
            </p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={create.isPending || trimmed.length === 0}>
              {create.isPending ? (
                <>
                  <Spinner className="size-4 animate-spin" aria-hidden="true" />
                  Creating&hellip;
                </>
              ) : (
                "Create"
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
