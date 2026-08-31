import { useId, useState } from "react";

import type { RestoreResult, TrashedAlbum } from "@/api/useTrash";
import {
  useEmptyAllTrash,
  useEmptyTrashAlbum,
  useRestoreTrash,
  useTrashList,
} from "@/api/useTrash";
import { Remove, Spinner, Warning } from "@/components/icons";
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
import { plural } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Settings → Trash: list deleted albums, restore one — exactly where it came
 * from, or re-filed by the current naming rules, per `restore_mode` — or empty
 * (forever). */
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
          {/* NOT "Restore puts one back as-is": only a `move_back` row goes back
           * to its own folder — an `import` row is re-filed by the current
           * naming rules. The promise lives per row now, so the header points
           * at it instead of making it for every row. */}
          <p className="text-muted-foreground text-sm">
            Deleted albums are moved here. Each row says where Restore will put
            it; emptying is permanent.
          </p>
          <p className="text-muted-foreground text-xs break-all">
            {trash_path}
          </p>
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
            onConfirm={(close) =>
              emptyAll.mutate(undefined, { onSuccess: close })
            }
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

/** Maps a restore outcome to its status message. */
function restoreResultMessage(result: RestoreResult): string {
  if (result.restored) {
    return "Restored to your library";
  }
  if (result.reason === "already_in_library") {
    return "Already in your library; not restored";
  }
  if (result.reason === "origin_occupied") {
    // A move-back that refused rather than merging: something is at the origin
    // again, so nothing moved. Say where the files ARE (still in Trash, so the
    // row and its Restore are unchanged) and the one thing that unblocks it —
    // the folder itself is named on the row's own "Goes back to" line.
    return "Its original folder exists again; nothing moved and the files are still in Trash. Clear that folder, then try again.";
  }
  return "Couldn’t restore";
}

/** What Restore will DO to this row, said before the user commits to it.
 *
 * `move_back` is a promise about WHERE (its own folder, named); `import` hands
 * over the backend's own sentence for why that is not on offer — three
 * distinct ones, so this must render whatever arrives rather than branch on
 * which. The warning icon is the glance-level tell and the run-in label the
 * readable one; neither carries the meaning alone. */
function RestoreOutlook({
  album,
  id,
}: Readonly<{ album: TrashedAlbum; id: string }>) {
  if (album.restore_mode === "move_back") {
    return (
      <p id={id} className="text-muted-foreground text-xs">
        <span className="text-foreground font-medium">Exact restore.</span>{" "}
        {album.origin ? (
          <>
            Goes back to <span className="break-words">{album.origin}</span>
          </>
        ) : (
          "Goes back to the folder it came from"
        )}
      </p>
    );
  }
  return (
    <p
      id={id}
      className="text-muted-foreground flex items-start gap-1.5 text-xs"
    >
      <Warning
        className="text-warning mt-0.5 size-3.5 shrink-0"
        aria-hidden="true"
      />
      {/* `min-w-0` is load-bearing, not tidiness. This span is a flex item of
       * the <p> above, so it defaults to `min-width: auto` and takes its floor
       * from the longest unbreakable token inside it. `overflow-wrap` on the
       * path chooses where LINES break; it does not lower that floor (only
       * `word-break: break-all` or this does). Without it an origin whose
       * segment has no separator to break at pushes the page sideways —
       * measured at 320px with an 85-character space-free path: scrollWidth
       * 633 vs clientWidth 305, and 305 with this class. */}
      <span className="min-w-0">
        <span className="text-warning font-medium">Approximate restore.</span>{" "}
        {album.restore_note}
        {album.origin && (
          // The wrap goes on the PATH ONLY, never the label around it, and it
          // is `break-words` so a path breaks at its separators instead of
          // mid-token. That is a READABILITY choice and nothing more: unlike
          // `break-all` it does not lower any ancestor's min-content width, so
          // it is only safe here because the span above carries `min-w-0` —
          // see that comment. Do not read this as "break-words is the safe
          // default"; every flex ancestor between this text and the scroll
          // container needs its own floor.
          <span className="mt-0.5 block">
            Was at <span className="break-words">{album.origin}</span>
          </span>
        )}
      </span>
    </p>
  );
}

/** One trashed album: name + meta, a Restore button, and a destructive Empty. */
function TrashRow({ album }: Readonly<{ album: TrashedAlbum }>) {
  const restore = useRestoreTrash();
  const empty = useEmptyTrashAlbum();
  const [result, setResult] = useState<RestoreResult | null>(null);

  const meta = [
    album.track_count
      ? `${album.track_count} ${plural(album.track_count, "track")}`
      : null,
    album.format,
    album.year?.toString() ?? null,
  ]
    .filter((bit): bit is string => Boolean(bit))
    .join(" · ");

  // 0 tracks = "nothing here produced a readable media Item", NOT "no audio"
  // (trash_manage.py) — beets' importer reads more than Item.from_path, so a
  // 0-track folder can still restore. Explain the uncertainty; never disable.
  const noTracks = album.track_count === 0;
  const exact = album.restore_mode === "move_back";
  const reasonId = useId();
  const outlookId = useId();

  // Row layout: `flex-wrap` + a 16rem basis on the text column, NOT `flex-1`
  // (whose basis is 0). The outlook line can carry a three-sentence note, and
  // squeezed into the ~200px left beside the two actions on a phone it wrapped
  // to nine lines. Below the basis the actions take their own row and the text
  // gets the full width; above it nothing wraps, and since the text column is
  // the ONLY growable child it still ends up exactly as wide as `flex-1` made
  // it — desktop is unchanged.
  return (
    <li className="flex flex-wrap items-center gap-3 px-4 py-3">
      <div className="flex min-w-0 grow basis-64 flex-col">
        <span className="truncate text-sm font-medium">
          {album.album_artist ?? "Unknown artist"} -{" "}
          {album.album ?? album.folder}
        </span>
        <span className="text-muted-foreground truncate text-xs">
          {meta || album.folder}
        </span>
        <div className="mt-1 flex flex-col gap-1">
          <RestoreOutlook album={album} id={outlookId} />
          {noTracks && (
            <span id={reasonId} className="text-muted-foreground text-xs">
              {exact
                ? // The husk this feature exists for: an audio-free folder WITH a
                  // record is moved back whole, so the old "may still work" hedge
                  // would now contradict the exact promise one line above. What is
                  // left to explain is the empty meta line, not the outcome.
                  "MusicDrop couldn’t read audio tags here — that’s why there are no track details, not a sign Restore won’t work."
                : "MusicDrop couldn’t read audio tags here — Restore may still work; Empty removes it permanently."}
            </span>
          )}
          {result && (
            <output
              className={cn(
                "text-xs",
                // A refusal must not read like the success directly above it.
                result.restored ? "text-muted-foreground" : "text-warning",
              )}
            >
              {restoreResultMessage(result)}
            </output>
          )}
        </div>
      </div>
      <Button
        variant="outline"
        size="sm"
        disabled={restore.isPending}
        aria-describedby={noTracks ? `${outlookId} ${reasonId}` : outlookId}
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
}: Readonly<{
  trigger: React.ReactNode;
  title: string;
  body: string;
  confirmLabel: string;
  pending: boolean;
  error: string | null;
  onConfirm: (close: () => void) => void;
}>) {
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
