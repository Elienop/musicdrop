import { useState } from "react";
import { useNavigate } from "react-router";

import { useDeleteArtist } from "@/api/useDeleteLibrary";
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
 * Trash action for an entire artist: confirm -> move EVERY album folder of the
 * artist to Trash + drop them -> navigate back to the roster. Reversible.
 */
export function DeleteArtistAction({
  name,
  albumCount,
}: {
  name: string;
  albumCount: number;
}) {
  const navigate = useNavigate();
  const del = useDeleteArtist();
  const [open, setOpen] = useState(false);

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>
        <IconAction label="Delete artist">
          <Remove weight="thin" className="size-10" aria-hidden="true" />
        </IconAction>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Move every album by this artist to Trash?</AlertDialogTitle>
          <AlertDialogDescription>
            All {albumCount} album{albumCount === 1 ? "" : "s"} by {name} —
            folders, art, and lyric sidecars — are moved to the Trash folder and
            removed from your library. Recoverable in Trash; Plex shows them as
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
