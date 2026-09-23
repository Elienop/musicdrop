import { useState } from "react";
import { useNavigate } from "react-router";

import { deleteRecovery, useDeleteArtist } from "@/api/useDeleteLibrary";
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
 * Trash action for an entire artist: confirm -> move every album's tracks, cover
 * art and MusicDrop's lyric files to Trash + drop the albums from the library ->
 * navigate back to the roster. Not the whole folders: anything else in them
 * stays. Restore re-imports the tracks and leaves the cover and the lyric files
 * in Trash (BACKLOG.md, open item), so the body promises "only the tracks" and
 * never a put-back.
 */
export function DeleteArtistAction({
  name,
  albumCount,
}: Readonly<{
  name: string;
  albumCount: number;
}> ) {
  const navigate = useNavigate();
  const del = useDeleteArtist();
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
        <IconAction label="Delete artist">
          <Remove weight="thin" className="size-10" aria-hidden="true" />
        </IconAction>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Move every album by this artist to Trash?</AlertDialogTitle>
          <AlertDialogDescription>
            Tracks, cover art, and lyrics from {albumCount} album
            {albumCount === 1 ? "" : "s"} by {name} move to Trash; other files
            stay in {albumCount === 1 ? "the folder" : "their folders"}. Restore
            re-imports only the tracks. Plex shows{" "}
            {albumCount === 1 ? "the album" : "the albums"} as unavailable until
            a rescan.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {del.isError && (
          // The twin of the album dialog's alert — same grid-item floor, same
          // measured overflow at 320px (DeleteAlbumAction.tsx has the numbers).
          <p className="text-destructive min-w-0 text-sm break-words" role="alert">
            {del.error.message}
            {/* The server's recovery hint, when it sent one — the twin of the
                album dialog's (DeleteAlbumAction.tsx). */}
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
              del.mutate(name, {
                onSuccess: () => {
                  setOpen(false);
                  void navigate("/artists");
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
