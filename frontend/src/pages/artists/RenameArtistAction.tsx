import { useState, type SubmitEvent } from "react";
import { useNavigate } from "react-router";

import type { components } from "@/api/schema";
import { useApplyArtistRename, usePreviewArtistRename } from "@/api/useArtistRename";
import { Edit, Spinner, Warning } from "@/components/icons";
import { IconAction } from "@/components/system/IconAction";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { plural } from "@/lib/format";

type ArtistRenameAlbumResult = components["schemas"]["ArtistRenameAlbumResult"];
type ArtistRenamePreview = components["schemas"]["ArtistRenamePreview"];

/** Renamed-but-damaged is NOT a clean success: nonzero write/move failures
 * must block the auto-navigate and be shown to the user, per-album counts and
 * all (the AlbumEditPanel failures alert is the honesty bar). */
function isClean(a: ArtistRenameAlbumResult): boolean {
  return a.outcome === "renamed" && a.write_failures === 0 && a.move_failures === 0;
}

/** The damage phrase for a renamed-but-damaged album: "renamed, but 3 files
 * failed to write and 2 files failed to move" (singular-aware, either or both). */
function damagePhrase(a: ArtistRenameAlbumResult): string {
  const parts: string[] = [];
  if (a.write_failures > 0)
    parts.push(`${a.write_failures} file${a.write_failures === 1 ? "" : "s"} failed to write`);
  if (a.move_failures > 0) parts.push(`${a.move_failures} file${a.move_failures === 1 ? "" : "s"} failed to move`);
  return `renamed, but ${parts.join(" and ")}`;
}

/** The idle-state apply verb: a merge preview flips "Apply" to "Merge". */
function applyButtonLabel(merging: boolean): string {
  return merging ? "Merge" : "Apply";
}

/** The apply verb while the rename is in flight: "Merging" or "Renaming". */
function applyPendingLabel(merging: boolean): string {
  return merging ? "Merging" : "Renaming";
}

/** The sr-only announcement once a fresh preview lands: the move totals, plus
 * the merge note when the target name already exists; empty before that. */
function previewAnnouncement(
  data: components["schemas"]["ArtistRenamePreview"] | undefined,
  totalMoves: number,
): string {
  if (!data) return "";
  const mergeNote = data.merge ? `, merges into ${data.new_name}` : "";
  return `Preview ready: ${data.albums.length} ${plural(data.albums.length, "album")}, ${totalMoves} ${plural(totalMoves, "file")} will move${mergeNote}`;
}

/** The apply-failure banner: which albums didn't rename cleanly and why, with
 * the manual "Go to renamed artist" escape hatch. */
function ResultFailureBanner({
  failures,
  onGoRenamed,
}: Readonly<{ failures: ArtistRenameAlbumResult[]; onGoRenamed: () => void }>) {
  if (failures.length === 0) return null;
  return (
    <StatusBanner
      tone="destructive"
      icon={Warning}
      action={
        <Button variant="outline" size="sm" type="button" onClick={onGoRenamed}>
          Go to renamed artist
        </Button>
      }
    >
      <p className="font-medium">
        {failures.length} album{failures.length === 1 ? "" : "s"} not cleanly renamed:
      </p>
      <ul className="mt-1 flex max-h-56 flex-col gap-0.5 overflow-y-auto pr-2">
        {failures.map((a) => (
          <li key={a.album_id} className="truncate">
            <span className="font-medium">{a.title}</span> —{" "}
            {a.outcome === "renamed" ? damagePhrase(a) : a.error ?? a.outcome}
          </li>
        ))}
      </ul>
    </StatusBanner>
  );
}

/** The preview panel: the move summary line, the merge consequence, the move
 * banner, the refusal warning, and the per-album list (data is non-null here,
 * guarded by the `preview.data &&` at the call site). */
function PreviewPanel({
  data,
  totalMoves,
  totalRefusals,
}: Readonly<{
  data: ArtistRenamePreview;
  totalMoves: number;
  totalRefusals: number;
}>) {
  return (
    <div className="flex min-w-0 flex-col gap-2 text-sm">
      {/* Summary line: always visible even though the list below scrolls. */}
      <p className="font-medium">
        {data.albums.length} album
        {data.albums.length === 1 ? "" : "s"} · {totalMoves} file
        {totalMoves === 1 ? "" : "s"} will move
      </p>

      {data.merge && (
        <p className="font-medium">
          &ldquo;{data.new_name}&rdquo; already exists with{" "}
          {data.merge.existing_album_count} album
          {data.merge.existing_album_count === 1 ? "" : "s"}. Applying merges
          the two artists into one; renaming back will not split them again.
        </p>
      )}

      {data.move_enabled ? (
        totalMoves > 0 && (
          <StatusBanner tone="warning" icon={Warning}>
            {totalMoves} file{totalMoves === 1 ? "" : "s"} will be moved on disk.
          </StatusBanner>
        )
      ) : (
        <p className="text-muted-foreground">
          Files will not move (moving is disabled in beets config).
        </p>
      )}

      {totalRefusals > 0 && (
        <p className="text-destructive font-medium">
          {totalRefusals} file{totalRefusals === 1 ? "" : "s"} cannot be moved (the
          destination name is already taken). Their tags will still be updated; these
          files keep their current names.
        </p>
      )}

      {/* Bounded + scrolling: Radix scroll-locks the page, so an
          unbounded list would push the footer off-screen unreachable. */}
      <ul className="flex max-h-72 flex-col gap-2 overflow-y-auto pr-2">
        {data.albums.map((a) => (
          <li key={a.album_id} className="min-w-0">
            <div className="flex justify-between gap-4">
              <span className="truncate">{a.title}</span>
              <span className="text-muted-foreground shrink-0 tabular-nums">
                {a.move_count} file{a.move_count === 1 ? "" : "s"} to move
              </span>
            </div>
            {a.refusals.length > 0 && (
              <ul className="mt-0.5 flex flex-col gap-0.5">
                {a.refusals.map((r) => (
                  <li key={r.item_id} className="text-destructive text-xs">
                    {r.track !== null && r.track !== undefined && r.track !== 0
                      ? `#${r.track} `
                      : ""}
                    {r.detail}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Rename an artist: fan the album edit (album artist ONLY — per-track artists
 * never follow) across every album, with the AlbumEditPanel gating discipline:
 * the apply verb is aria-disabled until a fresh Preview exists, and any
 * keystroke re-gates. Buttons never go `disabled` (focus would strand on
 * <body>); busy is a non-visual channel (aria-disabled/aria-busy + spinner).
 */
export function RenameArtistAction({ name }: Readonly<{ name: string }>) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [newName, setNewName] = useState(name);
  const preview = usePreviewArtistRename();
  const apply = useApplyArtistRename();

  const trimmed = newName.trim();
  const nameNeedsChange = trimmed !== "" && trimmed !== name;
  // Preview is gated while EITHER mutation is in flight: no second preview
  // racing the apply, no preview of a half-renamed library.
  const canPreview = nameNeedsChange && !preview.isPending && !apply.isPending;
  const canApply = !!preview.data && !apply.isPending;
  const merging = !!preview.data?.merge;

  const previewAlbums = preview.data?.albums ?? [];
  const totalMoves = previewAlbums.reduce((sum, a) => sum + a.move_count, 0);
  const totalRefusals = previewAlbums.reduce((sum, a) => sum + a.refusals.length, 0);

  function reset() {
    setNewName(name);
    preview.reset();
    apply.reset();
  }

  function onNameChange(value: string) {
    setNewName(value);
    // A changed name invalidates the preview: Apply must never run on a
    // preview computed for a different target.
    preview.reset();
    apply.reset();
  }

  const result = apply.data;
  const failures = result ? result.albums.filter((a) => !isClean(a)) : [];

  function onSubmit(e: SubmitEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!canPreview) return;
    preview.mutate({ name, new_name: trimmed });
  }

  function onApply() {
    apply.mutate(
      { name, new_name: trimmed },
      {
        onSuccess: (res) => {
          if (res.albums.every(isClean)) {
            setOpen(false);
            void navigate(`/artists/${encodeURIComponent(res.new_name)}`);
          } else {
            // The preview was computed before the move; it must not enable a
            // second Apply on a half-renamed library — the user re-previews.
            // The failure banner stays (it renders from apply.data).
            preview.reset();
          }
        },
      },
    );
  }

  function goRenamed() {
    if (!result) return;
    setOpen(false);
    void navigate(`/artists/${encodeURIComponent(result.new_name)}`);
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) reset();
      }}
    >
      <DialogTrigger asChild>
        <IconAction label="Rename artist">
          <Edit weight="thin" className="size-10" aria-hidden="true" />
        </IconAction>
      </DialogTrigger>
      <DialogContent
        // Corner X hides while the apply is in flight; Esc and outside-click
        // are swallowed either way so a dismissal can't race the rename.
        showCloseButton={!apply.isPending}
        onEscapeKeyDown={(e) => {
          if (apply.isPending) e.preventDefault();
        }}
        onInteractOutside={(e) => {
          if (apply.isPending) e.preventDefault();
        }}
      >
        <DialogHeader>
          <DialogTitle>Rename {name}</DialogTitle>
          <DialogDescription>
            Changes the album artist on every album by this artist and files the
            folders under the new name. Track artists are not touched.
          </DialogDescription>
        </DialogHeader>

        {/* Always mounted: fills on preview success, empties on reset. */}
        <output className="sr-only">
          {previewAnnouncement(preview.data, totalMoves)}
        </output>

        <form
          // min-w-0: DialogContent is a single-column grid whose min-width
          // defaults to its content; without this the long titles below would
          // push the form wider than the dialog box.
          className="flex min-w-0 flex-col gap-3"
          onSubmit={onSubmit}
        >
          <div className="flex flex-col gap-1">
            <label htmlFor="rename-artist-name" className="text-sm font-medium">
              New name
            </label>
            <Input
              id="rename-artist-name"
              value={newName}
              onChange={(e) => {
                if (apply.isPending) return;
                onNameChange(e.target.value);
              }}
              onFocus={(e) => e.currentTarget.select()}
              aria-disabled={apply.isPending || undefined}
              className="aria-disabled:opacity-50"
            />
            {!nameNeedsChange && (
              <p className="text-muted-foreground text-xs">
                Enter a different name to preview.
              </p>
            )}
          </div>

          {preview.isError && (
            <StatusBanner tone="destructive" icon={Warning}>
              {preview.error.message}
            </StatusBanner>
          )}

          {preview.data && (
            <PreviewPanel data={preview.data} totalMoves={totalMoves} totalRefusals={totalRefusals} />
          )}

          {apply.isError && (
            <StatusBanner tone="destructive" icon={Warning}>
              {apply.error.message}
            </StatusBanner>
          )}

          {result && <ResultFailureBanner failures={failures} onGoRenamed={goRenamed} />}

          <DialogFooter aria-busy={preview.isPending || apply.isPending}>
            <Button
              type="button"
              variant="outline"
              aria-disabled={apply.isPending || undefined}
              className="aria-disabled:opacity-50"
              onClick={() => {
                if (apply.isPending) return;
                setOpen(false);
              }}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="outline"
              aria-disabled={!canPreview || undefined}
              className="aria-disabled:opacity-50"
            >
              {preview.isPending ? (
                <>
                  <Spinner className="size-4 animate-spin" aria-hidden="true" />
                  Previewing&hellip;
                </>
              ) : (
                "Preview"
              )}
            </Button>
            <Button
              type="button"
              aria-disabled={!canApply || undefined}
              className="aria-disabled:opacity-50"
              onClick={() => {
                if (!canApply) return;
                onApply();
              }}
            >
              {apply.isPending ? (
                <>
                  <Spinner className="size-4 animate-spin" aria-hidden="true" />
                  {applyPendingLabel(merging)}&hellip;
                </>
              ) : (
                applyButtonLabel(merging)
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
