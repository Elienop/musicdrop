import type { DuplicateAction, DuplicatePrompt } from "@/api/useImport";
import {
  Close,
  Duplicates,
  Merge as MergeIcon,
  Replace as ReplaceIcon,
  Spinner,
  type AppIcon,
} from "@/components/icons";
import { ReleaseInfo } from "@/components/albums/ReleaseInfo";
import { MergePreviewTable } from "@/components/import/MergePreviewTable";
import { CoverArt } from "@/components/system/CoverArt";
import { Button } from "@/components/ui/button";

type IncomingAlbum = DuplicatePrompt["incoming"];
type ExistingAlbum = DuplicatePrompt["existing"][number];

/**
 * The duplicate comparison — h1 ("Already in your library"), explanation,
 * incoming panel beside every existing copy. `incomingCoverUrl` is the live
 * job's embedded-art URL on the import flow and null on the bank flow (the
 * art endpoint is job-scoped; existing-copy covers come from the library and
 * render on both). Pure render over a `DuplicatePrompt`.
 */
export function DuplicateComparison({
  prompt,
  incomingCoverUrl,
}: {
  prompt: DuplicatePrompt;
  incomingCoverUrl: string | null;
}) {
  return (
    <>
      <div className="flex flex-col gap-1">
        {/* THE page h1 — decision screens own their h1 directly (the Task-7
            detail-page idiom); tabIndex -1 keeps RouteAnnouncer's contract. */}
        <h1 tabIndex={-1} className="font-display text-display font-semibold tracking-tight">
          Already in your library
        </h1>
        <p className="text-muted-foreground text-sm">
          This album matches one you already have. Choose what to do before
          importing.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Panel
          id="incoming"
          heading="Importing (new)"
          accent
          album={prompt.incoming}
          coverUrl={incomingCoverUrl}
        />
        {prompt.existing.map((album) => (
          <Panel
            key={album.album_id}
            id={`existing-${album.album_id}`}
            heading="Already in library"
            album={album}
            coverUrl={`/api/albums/${album.album_id}/cover`}
          />
        ))}
      </div>

      {/* Per-track library-vs-import comparison — makes Merge/Replace/Skip
          legible. Absent for an as-is duplicate (no matched release to anchor). */}
      {prompt.merge_preview && <MergePreviewTable preview={prompt.merge_preview} />}
    </>
  );
}

/**
 * The bank-context footnote body for the four duplicate actions. Kept as a
 * shared constant so Task 2 can render the same text under the control bar.
 */
export const DUPLICATE_FOOTNOTE_BANK =
  "Keep both imports alongside the existing copy · Replace moves the old copy to Trash (reversible) · Merge combines them into your library.";

/**
 * beets' four duplicate actions as one flat button row — Skip new / Keep both
 * / Replace old / Merge. Reusable: `DuplicateActions` wraps it in the sticky
 * footnote shell; Task 2 drops it into the control bar's cluster slot.
 *
 * The caller owns the mutation: `onDecide(action)`; `pending` puts the spinner
 * on the clicked button; `busy` disables the row while a submission is in
 * flight. `describedBy` is the id of the footnote the buttons point at.
 */
export function DuplicateActionRow({
  pending,
  busy,
  onDecide,
  describedBy,
}: {
  pending: DuplicateAction | null;
  busy: boolean;
  onDecide: (action: DuplicateAction) => void;
  describedBy?: string;
}) {
  return (
    <>
      {/* None of the four is the preferred choice — they sit as one neutral
          peer row. Replace included: it moves the old copy to the reversible
          Trash (the footnote says so), so a warning tint would overstate the
          risk and discourage what is often the normal choice. */}
      <Button
        variant="outline"
        size="sm"
        disabled={busy}
        aria-describedby={describedBy}
        onClick={() => onDecide("skip_new")}
      >
        <ActionIcon action="skip_new" pending={pending} icon={Close} /> Skip new
      </Button>
      <Button
        variant="outline"
        size="sm"
        disabled={busy}
        aria-describedby={describedBy}
        onClick={() => onDecide("keep_both")}
      >
        <ActionIcon action="keep_both" pending={pending} icon={Duplicates} /> Keep both
      </Button>
      <Button
        variant="outline"
        size="sm"
        disabled={busy}
        aria-describedby={describedBy}
        onClick={() => onDecide("replace")}
      >
        <ActionIcon action="replace" pending={pending} icon={ReplaceIcon} /> Replace old
      </Button>
      <Button
        variant="outline"
        size="sm"
        disabled={busy}
        aria-describedby={describedBy}
        onClick={() => onDecide("merge")}
      >
        <ActionIcon action="merge" pending={pending} icon={MergeIcon} />
        Merge
      </Button>
    </>
  );
}

/**
 * beets' four duplicate actions in the sticky bottom bar — Skip new / Keep
 * both / Replace old / Merge — with the footnote. The caller owns the
 * mutation: `onDecide(action)`; `pending` puts the spinner on the clicked
 * button; `busy` disables the row while a submission is in flight.
 *
 * `context` tunes the Merge wording: on the live attended import beets' 'm'
 * rebuilds the album and re-runs the match, so it "reappears as a normal
 * review"; on the bank the merge is directive-driven and TERMINAL (the row
 * settles to done), so that promise would be false — bank says it merges into
 * the library, full stop.
 */
export function DuplicateActions({
  pending,
  busy,
  onDecide,
  context = "live",
}: {
  pending: DuplicateAction | null;
  busy: boolean;
  onDecide: (action: DuplicateAction) => void;
  context?: "live" | "bank";
}) {
  return (
    <>
      <div className="bg-background/80 sticky bottom-0 z-10 -mx-2 flex flex-wrap items-center gap-2 border-t px-2 py-3 backdrop-blur">
        <DuplicateActionRow
          pending={pending}
          busy={busy}
          onDecide={onDecide}
          describedBy="duplicate-footnote"
        />
      </div>
      <p id="duplicate-footnote" className="text-muted-foreground text-xs">
        {context === "bank"
          ? DUPLICATE_FOOTNOTE_BANK
          : "Keep both imports alongside the existing copy · Replace moves the old copy to Trash (reversible) · Merge combines them, then reappears as a normal review."}
      </p>
    </>
  );
}

/** The clicked button shows a spinner in place of its own icon while the
 * resolution is in flight, so progress reads on the button the user pressed
 * (not only on Merge). `pending` is the action currently being submitted. */
function ActionIcon({
  action,
  pending,
  icon: Icon,
}: {
  action: DuplicateAction;
  pending: DuplicateAction | null;
  icon: AppIcon;
}) {
  if (pending === action) {
    return <Spinner className="animate-spin" aria-hidden="true" />;
  }
  return <Icon aria-hidden="true" />;
}

export function Panel({
  id,
  heading,
  album,
  coverUrl,
  accent = false,
}: {
  id: string;
  heading: string;
  album: IncomingAlbum | ExistingAlbum;
  coverUrl: string | null;
  accent?: boolean;
}) {
  const headingId = `panel-heading-${id}`;
  const meta = [
    album.year?.toString() ?? null,
    `${album.track_count} ${album.track_count === 1 ? "track" : "tracks"}`,
    album.format,
    album.bitrate_kbps ? `${album.bitrate_kbps} kbps` : null,
  ].filter((b): b is string => Boolean(b));
  return (
    <section
      aria-labelledby={headingId}
      className="border-border flex flex-col gap-3 rounded-xl border p-4"
    >
      <p
        id={headingId}
        className={
          accent
            ? "text-primary-light text-xs font-medium tracking-wide uppercase"
            : "text-muted-foreground text-xs font-medium tracking-wide uppercase"
        }
      >
        {heading}
      </p>
      <div className="flex gap-4">
        <div className="w-48 shrink-0">
          <CoverArt src={coverUrl} className="w-full rounded-lg" />
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5 self-center">
          <p className="truncate font-medium">{album.album ?? "Unknown album"}</p>
          <p className="text-muted-foreground truncate text-sm">
            {album.album_artist ?? "Unknown artist"}
          </p>
          <p className="text-muted-foreground text-sm">{meta.join(" · ")}</p>
          {album.release && (
            <div className="text-muted-foreground flex flex-col gap-0.5 text-xs">
              <ReleaseInfo release={album.release} />
            </div>
          )}
          <p
            className="text-muted-foreground truncate font-mono text-xs"
            title={album.folder}
          >
            {album.folder}
          </p>
        </div>
      </div>
    </section>
  );
}
