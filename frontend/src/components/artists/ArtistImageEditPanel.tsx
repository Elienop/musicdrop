import { useEffect, useRef, useState } from "react";

import { Error as ErrorIcon, Reset, Upload } from "@/components/icons";

import { useResetArtistImageOverride, useUploadArtistImageOverride } from "@/api/useArtistImage";
import { Button } from "@/components/ui/button";

const ACCEPTED_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const MAX_BYTES = 10 * 1024 * 1024;

type Pending = { objectUrl: string; blob: Blob };

/** Artist-header override editor: upload a custom portrait or reset to the
 * automatic one. Modeled on CoverEditPanel (object-URL preview + leak guard). */
export function ArtistImageEditPanel({
  name,
  onSaved,
  onClose,
}: {
  name: string;
  onSaved: () => void;
  onClose: () => void;
}) {
  const [pending, setPending] = useState<Pending | null>(null);
  const [pickError, setPickError] = useState<string | null>(null);
  const upload = useUploadArtistImageOverride(name);
  const reset = useResetArtistImageOverride(name);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!pending) return;
    return () => URL.revokeObjectURL(pending.objectUrl);
  }, [pending]);

  const setPreview = (next: Pending) => {
    if (pending) URL.revokeObjectURL(pending.objectUrl);
    setPending(next);
  };

  const onPickFile = (file: File) => {
    if (!ACCEPTED_TYPES.includes(file.type)) {
      setPickError("That file isn't an image we can use — pick a PNG, JPEG, GIF, or WebP.");
      return;
    }
    if (file.size > MAX_BYTES) {
      setPickError("That image is over 10 MB — pick a smaller file.");
      return;
    }
    setPickError(null);
    setPreview({ objectUrl: URL.createObjectURL(file), blob: file });
  };

  const onSave = () => {
    if (!pending) return;
    upload.mutate(pending.blob, {
      onSuccess: () => {
        if (pending) URL.revokeObjectURL(pending.objectUrl);
        setPending(null);
        onSaved();
        onClose();
      },
    });
  };

  const onReset = () => {
    reset.mutate(undefined, {
      onSuccess: () => {
        onSaved();
        onClose();
      },
    });
  };

  return (
    <section aria-label="Edit artist image" className="flex flex-col gap-3 rounded-lg border p-4">
      <p className="text-muted-foreground text-sm">
        Upload a custom portrait for {name}, or reset to the automatic one.
      </p>
      {!pending && (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" onClick={() => fileInputRef.current?.click()}>
            <Upload className="size-4" aria-hidden="true" /> Upload an image…
          </Button>
          <Button variant="secondary" onClick={onReset} disabled={reset.isPending}>
            <Reset className="size-4" aria-hidden="true" />
            {reset.isPending ? "Resetting…" : "Reset to auto"}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            aria-label="Upload artist image"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onPickFile(f);
              e.target.value = "";
            }}
          />
        </div>
      )}

      {pickError && <Notice>{pickError}</Notice>}
      {reset.isError && <Notice>{reset.error.message}</Notice>}

      {pending && (
        <div className="flex flex-col gap-3">
          <img
            src={pending.objectUrl}
            alt="Artist image preview"
            className="bg-muted size-40 rounded-xl object-cover shadow-sm"
          />
          {upload.isError && <Notice>{upload.error.message}</Notice>}
          <div className="flex gap-2">
            <Button onClick={onSave} disabled={upload.isPending}>
              {upload.isPending ? "Saving…" : "Use this image"}
            </Button>
            <Button variant="ghost" onClick={() => setPending(null)} disabled={upload.isPending}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

/** Inline error notice (matches CoverEditPanel's house recipe). */
function Notice({ children }: { children: React.ReactNode }) {
  return (
    <div
      role="alert"
      className="border-destructive/40 bg-destructive/5 text-foreground flex items-start gap-2 rounded-md border p-3 text-sm"
    >
      <ErrorIcon className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </div>
  );
}
