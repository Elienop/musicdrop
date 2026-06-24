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
 * Trash action for a whole album: confirm -> move the album's entire folder
 * (tracks + art + lyric sidecars) to Trash + drop it -> navigate to the artist
 * page (this album is gone). Destructive but reversible. The Action button
 * preventDefaults so the dialog stays open showing "Moving…" until the move
 * resolves, then closes on success.
 */
export function DeleteAlbumAction({ album }: { album: AlbumDetail }) {
  const navigate = useNavigate();
  const del = useDeleteAlbum();
  const [open, setOpen] = useState(false);

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>
        <IconAction label="Delete album">
          <Remove weight="thin" className="size-10" aria-hidden="true" />
        </IconAction>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Move this album to Trash?</AlertDialogTitle>
          <AlertDialogDescription>
            The whole album folder — tracks, cover art, and the lyric sidecars —
            is moved to the Trash folder and removed from your library. It stays
            recoverable in Trash; Plex shows it as unavailable until a rescan.
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
