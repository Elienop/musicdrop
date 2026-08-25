import { useEffect, useMemo, useRef, useState } from "react";

import { Error as ErrorIcon, Info, Reset, Search, Upload } from "@/components/icons";

import { useArtistArtSettings } from "@/api/useArtistArt";
import {
  useArtistImageSettings,
  useArtistImageSources,
  useFetchArtistImage,
  useResetArtistImage,
  useSetArtistImageFromUrl,
  useUploadArtistImageOverride,
  type ArtistImageSourceId,
  type FetchedArtistImage,
} from "@/api/useArtistImage";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Skeleton } from "@/components/ui/skeleton";

const ACCEPTED_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const MAX_BYTES = 10 * 1024 * 1024;

type Pending = { objectUrl: string; blob: Blob; source: string | null };

/** Artist-header image editor: fetch a portrait from ONE named source, upload
 * your own, paste a link, or reset to automatic.
 *
 * Keyed on `name`, and that is load-bearing rather than tidy. Every piece of
 * state below is ABOUT one artist — a pending candidate above all — while the
 * route (`/artists/:artistName`, unkeyed) reconciles the same element when only
 * the param changes. Without this key, viewing artist A, fetching a preview and
 * pressing Back leaves A's photograph on screen under B's heading, and "Use
 * this image" POSTs A's bytes to `override?name=B`. The key lives here, not at
 * the call site, so the guard travels with the component instead of depending
 * on every future caller remembering it; the unmount cleanup releases the
 * abandoned candidate's object URL on the way out. */
export function ArtistImageEditPanel(props: Readonly<{
  name: string;
  onSaved: () => void;
  onClose: () => void;
}> ) {
  return <ArtistImageEditPanelForArtist key={props.name} {...props} />;
}

/** Modeled on CoverEditPanel — same fetch → preview → approve shape, same
 * object-URL leak guard — because the approved bytes are the bytes installed:
 * "Use this image" posts the very blob the preview holds, so nothing can
 * substitute a different image in between.
 *
 * Why the source is named rather than a bare "try again": every source picks
 * deterministically (fanart.tv takes the most-liked portrait, Spotify and
 * Deezer the most popular verified match), so re-running the automatic chain
 * returns the identical image. Addressing a source by name is the only thing
 * that changes the answer, and the copy has to say so or the control reads as
 * a refresh that does nothing. */
function ArtistImageEditPanelForArtist({
  name,
  onSaved,
  onClose,
}: Readonly<{
  name: string;
  onSaved: () => void;
  onClose: () => void;
}> ) {
  const [pending, setPending] = useState<Pending | null>(null);
  const [pickError, setPickError] = useState<string | null>(null);
  // ONE outcome channel for every non-error result — a source answering
  // "nothing", a reset reporting what it cleared, a portrait arriving. It is
  // announced from a region that is always mounted (below), so collapsing them
  // also means there is exactly one such region to keep alive.
  const [note, setNote] = useState<string | null>(null);
  const [didReset, setDidReset] = useState(false);
  const [picked, setPicked] = useState<ArtistImageSourceId | null>(null);
  const [url, setUrl] = useState("");
  const upload = useUploadArtistImageOverride(name);
  const reset = useResetArtistImage(name);
  const fromUrl = useSetArtistImageFromUrl(name);
  const fetchImage = useFetchArtistImage(name);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const fetchButtonRef = useRef<HTMLButtonElement>(null);
  const uploadButtonRef = useRef<HTMLButtonElement>(null);
  const returningFromPreview = useRef(false);

  // "Artist images are on" is not one question: the fetch route accepts when
  // EITHER the image toggle or the write-to-library toggle is on (main.py
  // composes that `or`), while /settings reports the image toggle alone. Gate
  // on the composed pair — `settings.enabled` by itself would hide a path that
  // works. `?? false` (off until known) rather than ArtistImage's
  // `=== false && === false`: for a BUTTON, flashing an enabled control during
  // load and retracting it is worse than showing it a beat late. A toggle
  // flipped in another tab mid-session still lands on the route's own 403.
  const imageSettings = useArtistImageSettings();
  const artSettings = useArtistArtSettings();
  const canFetch = (imageSettings.data?.enabled ?? false) || (artSettings.data?.enabled ?? false);
  // The sources query has no empty-name guard and the server declares
  // `min_length=1`, so an empty name must not be asked about at all.
  const sources = useArtistImageSources(name, canFetch && name.length > 0);

  const all = useMemo(() => sources.data?.sources ?? [], [sources.data]);
  const available = useMemo(() => all.filter((s) => s.available), [all]);
  // Configured but unusable for THIS artist (fanart.tv is MBID-keyed). Shown
  // with its reason rather than hidden: hiding it says "this source does not
  // exist here", which is a different and wrong statement. Sources with no
  // credentials never reach the client.
  const blocked = useMemo(() => all.filter((s) => !s.available), [all]);
  // Self-correcting: a picked source that drops out of the list on a refetch
  // falls back to the chain's first available one rather than going stale.
  const activeSource = available.find((s) => s.id === picked) ?? available[0] ?? null;

  // Revoke the live preview URL whenever it is replaced or the panel unmounts,
  // so a rejected candidate never leaks. Exactly one live object URL per panel.
  useEffect(() => {
    if (!pending) return;
    return () => URL.revokeObjectURL(pending.objectUrl);
  }, [pending]);

  // Fetching swaps the whole form out for the preview (and Discard swaps it
  // back), so the button that was just activated is unmounted under the user's
  // focus. Move focus to whatever replaced it instead of dropping to <body>.
  useEffect(() => {
    if (pending) {
      previewRef.current?.focus();
      return;
    }
    if (returningFromPreview.current) {
      returningFromPreview.current = false;
      (fetchButtonRef.current ?? uploadButtonRef.current)?.focus();
    }
  }, [pending]);

  const setPreview = (next: Pending) => {
    if (pending) URL.revokeObjectURL(pending.objectUrl);
    setPending(next);
  };

  const clearNotices = () => {
    setNote(null);
    setDidReset(false);
    setPickError(null);
    // The mutation's own error state is a notice too: without this a failed
    // fetch's red alert sat beside the success line of whatever the user did
    // next. Every entry point clears everything, rather than each remembering
    // its own subset.
    fetchImage.reset();
  };

  const onPickFile = (file: File) => {
    clearNotices();
    if (!ACCEPTED_TYPES.includes(file.type)) {
      setPickError("That file isn’t an image we can use. Pick a PNG, JPEG, GIF, or WebP.");
      return;
    }
    if (file.size > MAX_BYTES) {
      setPickError("That image is over 10 MB. Pick a smaller file.");
      return;
    }
    setPickError(null);
    setPreview({ objectUrl: URL.createObjectURL(file), blob: file, source: "your file" });
  };

  const onFetch = () => {
    if (!activeSource) return;
    clearNotices();
    // Captured at call time: the list can refetch while this is in flight, and
    // the caption must name the source the bytes actually came from.
    const asked = activeSource;
    fetchImage.mutate(asked.id, {
      onSuccess: (result: FetchedArtistImage) => {
        // A source that genuinely has nothing is an ANSWER, not a failure — the
        // hook already separates it from the 502 an outage produces, which
        // surfaces below as an error saying "try again".
        if (!result.found) {
          setNote(result.reason);
          return;
        }
        // The hook hands back bytes only; the URL is minted here so a fetch the
        // user navigated away from never creates one. See useArtistImage.ts.
        // `X-Art-Source` is unreadable cross-origin, and this caption is the
        // only thing on the preview saying WHERE the image came from — so fall
        // back to the label of the source we asked, which cannot be wrong.
        const from = result.source ?? asked.label;
        setPreview({ objectUrl: URL.createObjectURL(result.blob), blob: result.blob, source: from });
        setNote(`Found a portrait from ${from}.`);
      },
    });
  };

  const onSave = () => {
    if (!pending) return;
    // The previewed bytes, not a second fetch: re-fetching on approve could
    // install a different image than the one the user just looked at.
    upload.mutate(pending.blob, {
      onSuccess: () => {
        if (pending) URL.revokeObjectURL(pending.objectUrl);
        setPending(null);
        onSaved();
        onClose();
      },
    });
  };

  const onDiscardPreview = () => {
    // Only the candidate goes; the panel stays open so another source can be
    // tried without reopening it.
    returningFromPreview.current = true;
    setNote("Preview discarded.");
    setPending(null);
  };

  const onReset = () => {
    clearNotices();
    reset.mutate(undefined, {
      onSuccess: (result) => {
        setNote(
          result.cleared_override || result.cleared_auto
            ? "Cleared. This artist’s portrait will be looked up again."
            : // Both false is NOT "nothing to do": an unwritable cache dir
              // swallows the unlink while the in-memory entry is dropped, so
              // what gets served can still have changed. Claim only the part
              // that is true either way.
              "This artist’s portrait will be looked up again.",
        );
        setDidReset(true);
        onSaved();
      },
    });
  };

  const onSetFromUrl = () => {
    clearNotices();
    fromUrl.mutate(url.trim(), {
      onSuccess: () => {
        onSaved();
        onClose();
      },
    });
  };

  return (
    <section aria-label="Edit artist image" className="flex flex-col gap-4 rounded-lg border p-4">
      <p className="text-muted-foreground text-sm">Choose the portrait for {name}.</p>

      {/* ALWAYS mounted, empty or not: a live region inserted in the same commit
          as its text is not reliably announced — assistive tech monitors regions
          that already exist. `min-h-5` keeps the swap from shifting layout.
          Same idiom as the host page's album count (ArtistAlbumsPage.tsx). */}
      <p role="status" className="text-muted-foreground min-h-5 text-sm">
        {note}
      </p>

      {!pending && (
        <div className="flex flex-col gap-4">
          {canFetch && (
            <div className="flex flex-col gap-2">
              <h3 className="text-sm font-medium">Fetch from a source</h3>
              <p className="text-muted-foreground text-sm">
                Each source always returns its own single best match, so picking a different
                source — not fetching again — is what changes the result.
              </p>
              {/* `isLoading`, NOT `isPending`: a DISABLED query reports
                  `isPending: true` forever (pending also means "never asked"),
                  so `isPending` here paints a skeleton that never resolves the
                  moment this subtree renders without its gate. `isLoading` is
                  pending AND fetching — the only shape that means "a request is
                  in the air". Measured, not inferred; pinned in
                  useArtistImage.test.tsx. */}
              {sources.isLoading ? (
                <Skeleton className="h-9 w-56" />
              ) : (
                available.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2">
                    <SegmentedControl
                      aria-label="Image source"
                      value={activeSource?.id ?? ""}
                      onChange={(value) => {
                        const hit = available.find((s) => s.id === value);
                        if (!hit) return;
                        setPicked(hit.id);
                        // Drop the previous source's answer — it says nothing
                        // about the one now selected.
                        setNote(null);
                        fetchImage.reset();
                      }}
                      options={available.map((s) => ({ value: s.id, label: s.label }))}
                    />
                    <Button
                      ref={fetchButtonRef}
                      variant="secondary"
                      onClick={onFetch}
                      disabled={!activeSource || fetchImage.isPending}
                    >
                      <Search className="size-4" aria-hidden="true" />
                      {fetchImage.isPending ? "Fetching…" : "Fetch"}
                    </Button>
                  </div>
                )
              )}
              {blocked.map((s) => (
                <p
                  key={s.id}
                  className="text-muted-foreground flex items-start gap-2 text-sm"
                >
                  <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                  <span>
                    <span className="font-medium">{s.label}</span> — {s.reason}.
                  </span>
                </p>
              ))}
              {/* The hook keeps 404 out of here, so anything that lands is a
                  real failure — a 502 reads "try again", never "nothing
                  found" — and the sentence is the server's own. */}
              {fetchImage.isError && <Notice>{fetchImage.error.message}</Notice>}
              {sources.isError && <Notice>Couldn’t load the image sources.</Notice>}
            </div>
          )}

          <div className="flex flex-col gap-2">
            <h3 className="text-sm font-medium">Use your own image</h3>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                ref={uploadButtonRef}
                variant="outline"
                onClick={() => fileInputRef.current?.click()}
              >
                <Upload className="size-4" aria-hidden="true" /> Upload an image…
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
            <label htmlFor="artist-image-url" className="text-muted-foreground text-sm">
              …or paste an image link
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                id="artist-image-url"
                type="url"
                aria-label="Image URL"
                placeholder="https://…/image.jpg"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                className="min-w-0 flex-1"
              />
              <Button onClick={onSetFromUrl} disabled={!url.trim() || fromUrl.isPending}>
                {fromUrl.isPending ? "Setting…" : "Set"}
              </Button>
            </div>
            {url.trim() && (
              <img
                src={url}
                alt="Image link preview"
                className="bg-muted size-40 rounded-xl object-cover shadow-sm"
                onError={(e) => {
                  e.currentTarget.style.display = "none";
                }}
                onLoad={(e) => {
                  e.currentTarget.style.display = "";
                }}
              />
            )}
            {fromUrl.isError && <Notice>{fromUrl.error.message}</Notice>}
          </div>

          {pickError && <Notice>{pickError}</Notice>}
          {reset.isError && <Notice>{reset.error.message}</Notice>}

          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" onClick={onReset} disabled={reset.isPending}>
              <Reset className="size-4" aria-hidden="true" />
              {reset.isPending ? "Resetting…" : "Reset to auto"}
            </Button>
            {/* After a reset there is nothing left to abandon, so the closing
                button stops offering to "cancel" work already done. */}
            <Button variant="ghost" onClick={onClose}>
              {didReset ? "Done" : "Cancel"}
            </Button>
          </div>
        </div>
      )}

      {pending && (
        <div ref={previewRef} tabIndex={-1} className="flex flex-col gap-3 outline-none">
          <img
            src={pending.objectUrl}
            alt="Artist image preview"
            className="bg-muted size-40 rounded-xl object-cover shadow-sm"
          />
          {pending.source && (
            <p className="text-muted-foreground text-sm">from {pending.source}</p>
          )}
          {upload.isError && <Notice>{upload.error.message}</Notice>}
          <div className="flex gap-2">
            <Button onClick={onSave} disabled={upload.isPending}>
              {upload.isPending ? "Saving…" : "Use this image"}
            </Button>
            {/* "Discard", not "Cancel": this drops the candidate and returns to
                the panel — it does not close the panel. */}
            <Button variant="ghost" onClick={onDiscardPreview} disabled={upload.isPending}>
              Discard
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

/** Inline error notice (matches CoverEditPanel's house recipe). */
function Notice({ children }: Readonly<{ children: React.ReactNode }>) {
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
