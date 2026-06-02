import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useFetchAlbumCover, useInstallAlbumCover, type FetchedCover } from "@/api/useAlbumCover";

type Pending = { objectUrl: string; blob: Blob; source: string | null };

export function CoverEditPanel({
  albumId,
  onInstalled,
  onClose,
}: {
  albumId: number;
  onInstalled: () => void;
  onClose: () => void;
}) {
  const [pending, setPending] = useState<Pending | null>(null);
  const [notFound, setNotFound] = useState(false);
  const fetchCover = useFetchAlbumCover(albumId);
  const installCover = useInstallAlbumCover(albumId);

  const onFetch = () => {
    setNotFound(false);
    fetchCover.mutate(undefined, {
      onSuccess: (r: FetchedCover) => {
        if (!r.found) {
          setNotFound(true);
          return;
        }
        setPending({ objectUrl: r.objectUrl, blob: r.blob, source: r.source });
      },
    });
  };

  const onPickFile = (file: File) => {
    setNotFound(false);
    setPending({ objectUrl: URL.createObjectURL(file), blob: file, source: "Your file" });
  };

  const onUse = () => {
    if (!pending) return;
    installCover.mutate(pending.blob, {
      onSuccess: () => {
        URL.revokeObjectURL(pending.objectUrl);
        setPending(null);
        onInstalled();
        onClose();
      },
    });
  };

  const onCancel = () => {
    if (pending) URL.revokeObjectURL(pending.objectUrl);
    setPending(null);
    onClose();
  };

  return (
    <section aria-label="Edit cover" className="flex flex-col gap-3 rounded-lg border p-4">
      {!pending && (
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="secondary" onClick={onFetch} disabled={fetchCover.isPending}>
            {fetchCover.isPending ? "Searching…" : "Fetch from sources"}
          </Button>
          <label className="text-sm font-medium">
            <span className="sr-only">Upload image</span>
            <Input
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              aria-label="Upload cover image"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) onPickFile(f);
              }}
            />
          </label>
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      )}

      {notFound && <p className="text-muted-foreground text-sm">No cover found for this album.</p>}
      {fetchCover.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn’t fetch a cover.
        </p>
      )}

      {pending && (
        <div className="flex flex-col gap-3">
          <img
            src={pending.objectUrl}
            alt="Cover preview"
            className="bg-muted size-40 rounded-xl object-cover shadow-sm"
          />
          {pending.source && (
            <p className="text-muted-foreground text-sm">Source: {pending.source}</p>
          )}
          {installCover.isError && (
            <p className="text-destructive text-sm" role="alert">
              {installCover.error.message}
            </p>
          )}
          <div className="flex gap-2">
            <Button onClick={onUse} disabled={installCover.isPending}>
              {installCover.isPending ? "Saving…" : "Use this cover"}
            </Button>
            <Button variant="ghost" onClick={onCancel} disabled={installCover.isPending}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
