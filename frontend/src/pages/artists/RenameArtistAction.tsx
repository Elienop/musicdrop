import { useState } from "react";
import { useNavigate } from "react-router";

import { useApplyArtistRename, usePreviewArtistRename } from "@/api/useArtistRename";
import { Edit } from "@/components/icons";
import { IconAction } from "@/components/system/IconAction";
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

/**
 * Rename an artist: fan the album edit (album artist ONLY — per-track artists
 * never follow) across every album, with the AlbumEditPanel gating discipline:
 * Apply is disabled until a fresh Preview exists, and any keystroke re-gates.
 */
export function RenameArtistAction({ name }: { name: string }) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [newName, setNewName] = useState(name);
  const preview = usePreviewArtistRename();
  const apply = useApplyArtistRename();

  const trimmed = newName.trim();
  const canPreview = trimmed !== "" && trimmed !== name && !preview.isPending;
  const canApply = !!preview.data && !apply.isPending;

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
  const failures = result?.albums.filter((a) => a.outcome !== "renamed") ?? [];

  function onApply() {
    apply.mutate(
      { name, new_name: trimmed },
      {
        onSuccess: (res) => {
          if (res.albums.every((a) => a.outcome === "renamed")) {
            setOpen(false);
            void navigate(`/artists/${encodeURIComponent(res.new_name)}`);
          }
        },
      },
    );
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
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Rename {name}</DialogTitle>
          <DialogDescription>
            Changes the album artist on every album by this artist and files the
            folders under the new name. Track artists are not touched.
          </DialogDescription>
        </DialogHeader>

        <label className="flex flex-col gap-2 text-sm">
          New name
          <Input
            value={newName}
            onChange={(e) => onNameChange(e.target.value)}
            disabled={apply.isPending}
          />
        </label>

        {preview.isError && (
          <p className="text-destructive text-sm" role="alert">
            {preview.error.message}
          </p>
        )}

        {preview.data && (
          <div className="flex flex-col gap-2 text-sm">
            {preview.data.merge && (
              <p className="font-medium">
                Merges into existing “{preview.data.new_name}” (
                {preview.data.merge.existing_album_count}{" "}
                {preview.data.merge.existing_album_count === 1 ? "album" : "albums"}).
              </p>
            )}
            {!preview.data.move_enabled && (
              <p className="text-muted-foreground">
                Files will not move (moving is disabled in beets config).
              </p>
            )}
            <ul className="flex flex-col gap-1">
              {preview.data.albums.map((a) => (
                <li key={a.album_id} className="flex justify-between gap-4">
                  <span className="truncate">{a.title}</span>
                  <span className="text-muted-foreground shrink-0 tabular-nums">
                    {a.move_count} {a.move_count === 1 ? "file" : "files"} to move
                    {a.refusals.length > 0 && `, ${a.refusals.length} refused`}
                  </span>
                </li>
              ))}
            </ul>
            {preview.data.albums.some((a) => a.refusals.length > 0) && (
              <p className="text-destructive">
                Some files would collide at their new name and will not be moved —
                their tags still change. See the album edit panel for details.
              </p>
            )}
          </div>
        )}

        {apply.isError && (
          <p className="text-destructive text-sm" role="alert">
            {apply.error.message}
          </p>
        )}

        {result && failures.length > 0 && (
          <div className="flex flex-col gap-1 text-sm" role="alert">
            <p className="font-medium">
              {failures.length} {failures.length === 1 ? "album" : "albums"} not renamed:
            </p>
            <ul className="flex flex-col gap-1">
              {failures.map((a) => (
                <li key={a.album_id}>
                  {a.title} — {a.error ?? a.outcome}
                </li>
              ))}
            </ul>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setOpen(false);
                void navigate(`/artists/${encodeURIComponent(result.new_name)}`);
              }}
            >
              Go to renamed artist
            </Button>
          </div>
        )}

        <DialogFooter>
          <Button
            variant="outline"
            disabled={!canPreview}
            onClick={() => preview.mutate({ name, new_name: trimmed })}
          >
            {preview.isPending ? "Previewing…" : "Preview"}
          </Button>
          <Button disabled={!canApply} onClick={onApply}>
            {apply.isPending ? "Renaming…" : "Apply"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
