import { AlertCircle, CheckCircle2, Info } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useFetchAlbumCover, useInstallAlbumCover, type FetchedCover } from "@/api/useAlbumCover";
import type { components } from "@/api/schema";

type CoverInstallResult = components["schemas"]["CoverInstallResult"];

type Pending = { objectUrl: string; blob: Blob; source: string | null };

/** Image types the cover endpoint accepts (mirrors the picker's `accept`). */
const ACCEPTED_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const MAX_BYTES = 10 * 1024 * 1024;

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
  const [pickError, setPickError] = useState<string | null>(null);
  const [installed, setInstalled] = useState<CoverInstallResult | null>(null);
  const fetchCover = useFetchAlbumCover(albumId);
  const installCover = useInstallAlbumCover(albumId);

  // Revoke the live preview URL whenever it is replaced or the panel unmounts
  // (e.g. the parent's "Cover" toggle), so a pending blob never leaks.
  useEffect(() => {
    if (!pending) return;
    return () => URL.revokeObjectURL(pending.objectUrl);
  }, [pending]);

  const setPreview = (next: Pending) => {
    // A new preview supersedes any prior one; release its URL first.
    if (pending) URL.revokeObjectURL(pending.objectUrl);
    setPending(next);
  };

  const onFetch = () => {
    setNotFound(false);
    setPickError(null);
    fetchCover.mutate(undefined, {
      onSuccess: (r: FetchedCover) => {
        if (!r.found) {
          setNotFound(true);
          return;
        }
        setPreview({ objectUrl: r.objectUrl, blob: r.blob, source: r.source });
      },
    });
  };

  const onPickFile = (file: File) => {
    setNotFound(false);
    fetchCover.reset();
    if (!ACCEPTED_TYPES.includes(file.type)) {
      setPickError("That file isn't an image we can use — pick a PNG, JPEG, GIF, or WebP.");
      return;
    }
    if (file.size > MAX_BYTES) {
      setPickError("That image is over 10 MB — pick a smaller file.");
      return;
    }
    setPickError(null);
    setPreview({ objectUrl: URL.createObjectURL(file), blob: file, source: "Your file" });
  };

  const onUse = () => {
    if (!pending) return;
    installCover.mutate(pending.blob, {
      onSuccess: (result) => {
        // Drop the preview (and its URL) but keep the panel open so the outcome
        // is visible; cache-bust the album art now, close on the user's "Done".
        if (pending) URL.revokeObjectURL(pending.objectUrl);
        setPending(null);
        setInstalled(result);
        onInstalled();
      },
    });
  };

  const onCancel = () => {
    setPending(null);
    onClose();
  };

  if (installed) {
    return (
      <section aria-label="Edit cover" className="flex flex-col gap-3 rounded-lg border p-4">
        <InstallOutcome result={installed} />
        <div>
          <Button onClick={onClose}>Done</Button>
        </div>
      </section>
    );
  }

  return (
    <section aria-label="Edit cover" className="flex flex-col gap-3 rounded-lg border p-4">
      {!pending && (
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="secondary" onClick={onFetch} disabled={fetchCover.isPending}>
            {fetchCover.isPending ? "Searching…" : "Fetch from sources"}
          </Button>
          <Input
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            aria-label="Upload cover image"
            className="w-auto"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onPickFile(f);
            }}
          />
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      )}

      {notFound && <p className="text-muted-foreground text-sm">No cover found for this album.</p>}
      {pickError && <Notice>{pickError}</Notice>}
      {fetchCover.isError && <Notice>Couldn’t fetch a cover.</Notice>}

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
          {installCover.isError && <Notice>{installCover.error.message}</Notice>}
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

/** Post-install confirmation: "Cover updated" plus any embed detail the backend
 * reports (embedded into files, or why embedding was skipped). */
function InstallOutcome({ result }: { result: CoverInstallResult }) {
  const detail =
    result.embed_detail ?? (result.embedded ? "Also embedded into the album's files." : null);
  return (
    <div className="flex flex-col gap-2 text-sm">
      <div
        role="status"
        className="border-primary/30 bg-primary/5 text-foreground flex items-start gap-2 rounded-md border p-3"
      >
        <CheckCircle2 className="text-primary mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <div className="flex flex-col gap-1">
          <span className="font-medium">Cover updated</span>
          {result.message && <span className="text-muted-foreground">{result.message}</span>}
        </div>
      </div>
      {detail && (
        <div className="text-muted-foreground flex items-start gap-2 rounded-md border p-3">
          <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{detail}</span>
        </div>
      )}
    </div>
  );
}

/** Inline error notice matching AlbumEditPanel's house recipe (rounded border +
 * p-3, semantic destructive tint, leading icon). */
function Notice({ children }: { children: React.ReactNode }) {
  return (
    <div
      role="alert"
      className="border-destructive/40 bg-destructive/5 text-foreground flex items-start gap-2 rounded-md border p-3 text-sm"
    >
      <AlertCircle className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </div>
  );
}
