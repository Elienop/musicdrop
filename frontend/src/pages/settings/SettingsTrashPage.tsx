import { useState } from "react";

import type { RestoreResult, TrashedAlbum } from "@/api/useTrash";
import {
  useEmptyAllTrash,
  useEmptyTrashAlbum,
  useRestoreTrash,
  useTrashList,
} from "@/api/useTrash";
import { Remove, Spinner } from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
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
import { Button } from "@/components/ui/button";

/** Settings → Trash: list deleted albums, restore one as-is, or empty (forever). */
export function SettingsTrashPage() {
  const { data, isPending, isError, refetch } = useTrashList();
  const emptyAll = useEmptyAllTrash();

  if (isPending) {
    return <p className="text-muted-foreground text-sm">Loading Trash…</p>;
  }
  if (isError) {
    return (
      <div className="flex flex-col items-start gap-2">
        <p className="text-destructive text-sm" role="alert">
          Couldn’t load Trash.
        </p>
        <Button variant="outline" size="sm" onClick={() => void refetch()}>
          Try again
        </Button>
      </div>
    );
  }

  const { albums, trash_path } = data;
  return (
    <section aria-label="Trash" className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="text-lg font-semibold">Trash</h2>
          <p className="text-muted-foreground text-sm">
            Deleted albums are moved here. Restore puts one back as-is; emptying
            is permanent.
          </p>
          <p className="text-muted-foreground text-xs break-all">{trash_path}</p>
        </div>
        {albums.length > 0 && (
          <ConfirmAction
            trigger={
              <Button variant="outline" size="sm" disabled={emptyAll.isPending}>
                Empty all
              </Button>
            }
            title="Empty the whole Trash?"
            body={`Permanently deletes every album in Trash (${albums.length}). This can’t be undone.`}
            confirmLabel="Empty all"
            pending={emptyAll.isPending}
            error={emptyAll.isError ? emptyAll.error.message : null}
            onConfirm={(close) => emptyAll.mutate(undefined, { onSuccess: close })}
          />
        )}
      </div>

      {albums.length === 0 ? (
        <EmptyState
          icon={Remove}
          title="Trash is empty"
          body="Albums you delete show up here, ready to restore or remove for good."
        />
      ) : (
        <ul className="border-border divide-border flex flex-col divide-y rounded-xl border">
          {albums.map((album) => (
            <TrashRow key={album.folder} album={album} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** One trashed album: name + meta, a Restore button, and a destructive Empty. */
function TrashRow({ album }: { album: TrashedAlbum }) {
  const restore = useRestoreTrash();
  const empty = useEmptyTrashAlbum();
  const [result, setResult] = useState<RestoreResult | null>(null);

  const meta = [
    album.track_count
      ? `${album.track_count} track${album.track_count === 1 ? "" : "s"}`
      : null,
    album.format,
    album.year?.toString() ?? null,
  ]
    .filter((bit): bit is string => Boolean(bit))
    .join(" · ");

  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-sm font-medium">
          {album.album_artist ?? "Unknown artist"} — {album.album ?? album.folder}
        </span>
        <span className="text-muted-foreground truncate text-xs">{meta || album.folder}</span>
        {result && (
          <span className="text-muted-foreground text-xs" role="status">
            {result.restored
              ? "Restored to your library"
              : result.reason === "already_in_library"
                ? "Already in your library — not restored"
                : "Couldn’t restore"}
          </span>
        )}
      </div>
      <Button
        variant="outline"
        size="sm"
        disabled={restore.isPending}
        onClick={() => restore.mutate(album.folder, { onSuccess: setResult })}
      >
        {restore.isPending ? (
          <>
            <Spinner className="animate-spin" aria-hidden="true" /> Restoring…
          </>
        ) : (
          "Restore"
        )}
      </Button>
      <ConfirmAction
        trigger={
          <IconAction label={`Empty ${album.album ?? album.folder}`}>
            <Remove weight="thin" className="size-10" aria-hidden="true" />
          </IconAction>
        }
        title="Delete permanently?"
        body="Permanently deletes this album’s files from Trash. This can’t be undone."
        confirmLabel="Delete"
        pending={empty.isPending}
        error={empty.isError ? empty.error.message : null}
        onConfirm={(close) => empty.mutate(album.folder, { onSuccess: close })}
      />
    </li>
  );
}

/** A trigger that opens an AlertDialog confirm; the action holds the dialog open
 * (preventDefault) until the mutation settles, then closes via the passed cb. */
function ConfirmAction({
  trigger,
  title,
  body,
  confirmLabel,
  pending,
  error,
  onConfirm,
}: {
  trigger: React.ReactNode;
  title: string;
  body: string;
  confirmLabel: string;
  pending: boolean;
  error: string | null;
  onConfirm: (close: () => void) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{body}</AlertDialogDescription>
        </AlertDialogHeader>
        {error && (
          <p className="text-destructive text-sm" role="alert">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            disabled={pending}
            onClick={(e) => {
              e.preventDefault();
              onConfirm(() => setOpen(false));
            }}
          >
            {pending ? "Working…" : confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
