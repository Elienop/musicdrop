import { useState } from "react";
import { useNavigate } from "react-router";

import type { AlbumDetail } from "@/api/useAlbum";
import { useDeleteAlbum } from "@/api/useDeleteLibrary";
import { Remove } from "@/components/icons";
import { IconAction } from "@/components/system/IconAction";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";

/**
 * Trash action for one album: confirm -> move the album's tracks, cover art and
 * MusicDrop's lyric files to Trash + drop the album from the library -> navigate
 * to the artist page (this album is gone). Anything else in the folder is left
 * alone, and the folder survives while something is still in it, so the body
 * must not promise the whole folder. Restore is not a put-back either: it
 * re-imports the tracks and leaves the cover and lyrics in Trash — hence "only
 * the tracks" (BACKLOG.md, open item). The Action button preventDefaults so the
 * dialog stays open showing "Moving…" until the move resolves.
 */
export function DeleteAlbumAction({ album }: Readonly<{ album: AlbumDetail }>) {
  const navigate = useNavigate();
  const del = useDeleteAlbum();
  const [open, setOpen] = useState(false);

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        // The mutation outlives the dialog, so clear a failed attempt's alert
        // on the way in rather than reopening onto it.
        if (next) del.reset();
        setOpen(next);
      }}
    >
      <AlertDialogTrigger asChild>
        <IconAction label="Delete album">
          <Remove weight="thin" className="size-10" aria-hidden="true" />
        </IconAction>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Move this album to Trash?</AlertDialogTitle>
          <AlertDialogDescription>
            Tracks, cover art, and lyrics move to Trash; other files stay in the
            folder. Restore re-imports only the tracks. Plex shows the album as
            unavailable until a rescan.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {del.isError && (
          <p className="text-destructive text-sm" role="alert">
            {del.error.message}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={del.isPending}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            disabled={del.isPending}
            onClick={(e) => {
              e.preventDefault();
              del.mutate(album.id, {
                onSuccess: () => {
                  setOpen(false);
                  void navigate(`/artists/${encodeURIComponent(album.album_artist)}`);
                },
              });
            }}
          >
            {del.isPending ? "Moving…" : "Move to Trash"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
