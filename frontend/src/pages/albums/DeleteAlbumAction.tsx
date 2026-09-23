import { useState } from "react";
import { useNavigate } from "react-router";

import type { AlbumDetail } from "@/api/useAlbum";
import { deleteRecovery, useDeleteAlbum } from "@/api/useDeleteLibrary";
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
          // `min-w-0` is the load-bearing half. This <p> is a GRID item of
          // AlertDialogContent, so it defaults to `min-width: auto` — its
          // min-content width, which a 150-character path makes larger than the
          // dialog. Measured at 320px: the <p> used 365 and painted to x=406,
          // clipping the title, body and buttons. `break-words` alone does not
          // lower that floor; it only chooses where lines break.
          <p className="text-destructive min-w-0 text-sm break-words" role="alert">
            {del.error.message}
            {/* The server's recovery hint, when it sent one — a half-done
                delete leaves files in Trash and says to retry BEFORE emptying
                it. Inside the same alert so it is announced with the failure,
                not as a second interruption. */}
            {deleteRecovery(del.error) !== null && (
              <span className="mt-1 block">{deleteRecovery(del.error)}</span>
            )}
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
