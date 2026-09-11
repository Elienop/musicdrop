import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  type DuplicateAlbum,
  type DuplicateGroup,
  type DuplicateMode,
  type DuplicatesOpError,
  type ResolveAllResult,
  useDuplicates,
  useResolveAllDuplicates,
  useResolveDuplicate,
} from "@/api/useDuplicates";
import { Resolved, Spinner } from "@/components/icons";
import { AlbumRow } from "@/components/system/AlbumRow";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
import { PageBody, PageHeader } from "@/components/system/PageHeader";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { plural } from "@/lib/format";
import { cn } from "@/lib/utils";

/** One group's resolve plan: the clamped keeper plus the loser album ids. */
type Decision = {
  group: DuplicateGroup;
  keep: number;
  removeIds: number[];
};

/** Both resolve mutations treat a 409 the same way: the library drifted under
 * us, so drop the duplicates query and let it refetch. */
function invalidateOn409(err: unknown, queryClient: QueryClient): void {
  if ((err as DuplicatesOpError).status === 409) {
    void queryClient.invalidateQueries({ queryKey: ["duplicates"] });
  }
}

/** A group's chosen keeper, clamped to current membership. A background refetch
 * can change a group's members while keeping its `suggested_keeper_id` (so the
 * card doesn't remount), which would leave a selection pointing at a vanished
 * album; falling back to the suggested keeper keeps remove-ids + the dialog
 * truthful. Shared by each card and the bulk action. */
function effectiveKeeperId(
  group: DuplicateGroup,
  selections: Record<number, number>,
): number {
  const chosen = selections[group.suggested_keeper_id];
  return group.members.some((m) => m.id === chosen)
    ? chosen
    : group.suggested_keeper_id;
}

export function DuplicatesPage() {
  const [mode, setMode] = useState<DuplicateMode>("strict");
  const { data, isPending, isError, refetch } = useDuplicates(mode);
  // Keeper choices live here (not per-card) so the bulk "Resolve all" can read
  // every group's selection. Keyed by the stable `suggested_keeper_id`.
  const [selections, setSelections] = useState<Record<number, number>>({});
  const [confirmAllOpen, setConfirmAllOpen] = useState(false);
  const [summary, setSummary] = useState<ResolveAllResult | null>(null);
  const resolveAll = useResolveAllDuplicates();
  const queryClient = useQueryClient();

  const groups = data?.groups ?? [];
  // Copies the bulk action moves: one keeper per group, the rest to Trash —
  // independent of WHICH keeper, so derive from the report counts.
  const moveCount = data ? data.album_count - data.group_count : 0;
  const showBulk = (data?.group_count ?? 0) >= 2;

  const decisions = groups.map((g) => {
    const keep = effectiveKeeperId(g, selections);
    return { group: g, keep, removeIds: g.members.filter((m) => m.id !== keep).map((m) => m.id) };
  });

  function onConfirmAll() {
    setSummary(null);
    resolveAll.mutate(
      { mode, groups: decisions.map((d) => ({ keep_album_id: d.keep, remove_album_ids: d.removeIds })) },
      {
        onSettled: () => setConfirmAllOpen(false),
        onSuccess: (res) => setSummary(res),
        onError: (err) => invalidateOn409(err, queryClient),
      },
    );
  }

  // Switching modes regroups the library — drop keeper overrides (a same
  // keeper-id can recur with different membership across modes) and the stale
  // summary so the bulk action can't carry a wrong choice over.
  function switchMode(m: DuplicateMode) {
    setMode(m);
    setSelections({});
    setSummary(null);
  }

  const allError = resolveAll.error as DuplicatesOpError | null;

  return (
    <PageBody>
      <PageHeader
        title="Duplicates"
        meta={
          data
            ? `${data.group_count} ${plural(data.group_count, "group")} · ${data.album_count} albums`
            : "Scanning your library…"
        }
        actions={
          <HeaderActions
            showBulk={showBulk}
            moveCount={moveCount}
            resolving={resolveAll.isPending}
            onOpenConfirm={() => setConfirmAllOpen(true)}
            mode={mode}
            onModeChange={switchMode}
          />
        }
      />

      {summary && <BulkResolveNote summary={summary} />}
      {allError && <ResolveAllErrorNote error={allError} />}

      {isPending && (
        <output className="text-muted-foreground flex items-center gap-2 text-sm">
          <Spinner className="size-4 animate-spin" aria-hidden="true" />
          Scanning library for duplicates&hellip;
        </output>
      )}
      {isError && (
        <ErrorState
          variant="inline"
          message="Couldn't load duplicates."
          onRetry={() => void refetch()}
        />
      )}
      {data && groups.length === 0 && (
        <EmptyState
          bordered
          icon={Resolved}
          title="No duplicate albums found"
          body="Your library is clean in this mode."
        />
      )}
      {data &&
        groups.map((group) => (
          <GroupCard
            key={group.suggested_keeper_id}
            group={group}
            mode={mode}
            keeperId={effectiveKeeperId(group, selections)}
            onChoose={(albumId) =>
              setSelections((s) => ({ ...s, [group.suggested_keeper_id]: albumId }))
            }
          />
        ))}

      <ConfirmAllDialog
        open={confirmAllOpen}
        onOpenChange={setConfirmAllOpen}
        moveCount={moveCount}
        groupCount={data?.group_count ?? 0}
        decisions={decisions}
        onConfirm={onConfirmAll}
      />
    </PageBody>
  );
}

/** Header controls: the bulk "Resolve all" action (only when ≥2 groups exist)
 * and the match-mode switcher. */
function HeaderActions({
  showBulk,
  moveCount,
  resolving,
  onOpenConfirm,
  mode,
  onModeChange,
}: Readonly<{
  showBulk: boolean;
  moveCount: number;
  resolving: boolean;
  onOpenConfirm: () => void;
  mode: DuplicateMode;
  onModeChange: (mode: DuplicateMode) => void;
}> ) {
  return (
    <>
      {showBulk && (
        <Button
          variant="destructive"
          disabled={resolving}
          onClick={onOpenConfirm}
        >
          {resolving ? (
            <>
              <Spinner className="animate-spin" aria-hidden="true" />
              Resolving&hellip;
            </>
          ) : (
            `Resolve all · ${moveCount} ${plural(moveCount, "copy", "copies")}`
          )}
        </Button>
      )}
      <SegmentedControl
        aria-label="Match mode"
        options={[
          { value: "strict", label: "Strict · MB-ID" },
          { value: "fuzzy", label: "Fuzzy · artist + title" },
        ]}
        value={mode}
        onChange={(v) => {
          const m: DuplicateMode = v === "fuzzy" ? "fuzzy" : "strict";
          onModeChange(m);
        }}
      />
    </>
  );
}

/** The skipped-groups side note for the moved-copies line — skipped groups
 * don't change the lead ("moved N copies"), so they trail as one clause. */
function skippedSuffix(skipped: number): string {
  return ` ${skipped} group${skipped === 1 ? "" : "s"} changed and ${
    skipped === 1 ? "was" : "were"
  } skipped; refreshed; re-check ${skipped === 1 ? "it" : "them"}.`;
}

/** The bulk-resolve outcome line. Two shapes: copies actually moved vs. all
 * groups skipped. */
function BulkResolveNote({ summary }: Readonly<{ summary: ResolveAllResult }>) {
  const skipped = summary.skipped_stale.length;
  const reexported = summary.playlists_reexported;
  return summary.moved_count > 0 ? (
    <output className="text-muted-foreground text-sm block">
      Moved {summary.moved_count} {summary.moved_count === 1 ? "copy" : "copies"} across{" "}
      {summary.group_count} {summary.group_count === 1 ? "group" : "groups"} to Trash.
      {/* Own sentence, and only when there was one: a move that touched no
          playlist should not spend a clause saying so. Sits ahead of the
          skipped caveat, which trails the whole outcome. */}
      {reexported > 0 && ` Re-exported ${reexported} ${plural(reexported, "playlist")}.`}
      {skipped > 0 && skippedSuffix(skipped)}
    </output>
  ) : (
    // All groups drifted since the scan (a normal 200 with nothing moved):
    // lead with the actionable part, not a "moved 0" that reads as a no-op.
    // No re-export clause here by construction: the backend collects dropped
    // item ids in the same step that counts a moved copy, so moved_count 0
    // means playlists_reexported 0 (app/beets/duplicates.py, resolve loop).
    <output className="text-sm block">
      Nothing moved; {skipped === 1 ? "the group" : `all ${skipped} groups`} changed
      since the scan and {skipped === 1 ? "was" : "were"} skipped. The report refreshed;
      re-check {skipped === 1 ? "it" : "them"}.
    </output>
  );
}

/** The bulk-resolve failure line: a 409 gets the cause-specific hint (see
 * `resolve409Message`), anything else the generic retry message. */
function ResolveAllErrorNote({ error }: Readonly<{ error: DuplicatesOpError }>) {
  return (
    <p className="text-destructive text-sm" role="alert">
      {error.status === 409
        ? resolve409Message(error)
        : "Resolve all failed. The Trash keeps any moved copies; refresh and retry."}
    </p>
  );
}

/** The bulk "Move all to Trash" confirmation: the per-group keeper and
 * move-count list plus the confirm action. */
function ConfirmAllDialog({
  open,
  onOpenChange,
  moveCount,
  groupCount,
  decisions,
  onConfirm,
}: Readonly<{
  open: boolean;
  onOpenChange: (open: boolean) => void;
  moveCount: number;
  groupCount: number;
  decisions: Decision[];
  onConfirm: () => void;
}> ) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            Move {moveCount} {moveCount === 1 ? "copy" : "copies"} across{" "}
            {groupCount} {groupCount === 1 ? "group" : "groups"} to Trash?
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="text-sm">
              Keeping one copy per group (the marked keeper). These move to the
              Trash folder (reversible; nothing is deleted):
              <ul className="mt-2 max-h-64 space-y-1 overflow-y-auto">
                {decisions.map((d) => {
                  const keeper = d.group.members.find((m) => m.id === d.keep);
                  return (
                    <li key={d.group.suggested_keeper_id} className="text-xs">
                      Keep <strong>{keeper?.title}</strong>
                      <span className="text-muted-foreground">
                        {" "}
                        - {keeper?.album_artist} · move {d.removeIds.length}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>Move all to Trash</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** Inline message for a 409 from resolve. Distinguishes the two backend causes
 * (import-active vs stale-group) from the flat `detail` string so the user gets
 * an actionable hint instead of one generic line. */
function resolve409Message(err: DuplicatesOpError): string {
  const detail = (err.body as { detail?: string } | null)?.detail ?? "";
  return detail.toLowerCase().includes("import")
    ? "Can't resolve while an import is running."
    : "This group changed; refreshing. Re-check the copies and retry.";
}

function GroupCard({
  group,
  mode,
  keeperId,
  onChoose,
}: Readonly<{
  group: DuplicateGroup;
  mode: DuplicateMode;
  keeperId: number;
  onChoose: (albumId: number) => void;
}> ) {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const resolve = useResolveDuplicate();
  const queryClient = useQueryClient();

  // `keeperId` is the page's clamped selection (always a current member);
  // derive this group's losers from it.
  const keeper = group.members.find((m) => m.id === keeperId);
  const removeIds = group.members.filter((m) => m.id !== keeperId).map((m) => m.id);

  function onConfirm() {
    resolve.mutate(
      { mode, keep_album_id: keeperId, remove_album_ids: removeIds },
      {
        onSettled: () => setConfirmOpen(false),
        onError: (err) => invalidateOn409(err, queryClient),
      },
    );
  }

  const opError = resolve.error as DuplicatesOpError | null;

  return (
    <div className="rounded-xl border p-4">
      <div className="text-muted-foreground mb-2 text-sm">
        Matched on <strong className="text-foreground">{group.match_reason}</strong> ·{" "}
        {group.members.length} copies
      </div>
      <ul className="divide-border divide-y">
        {group.members.map((album) => (
          <MemberRow
            key={album.id}
            album={album}
            // Shared per-GROUP radio name so the copies form one radio group
            // (single-select + arrow-key nav). Stable across re-render.
            name={`keeper-${group.suggested_keeper_id}`}
            checked={album.id === keeperId}
            onChoose={() => onChoose(album.id)}
          />
        ))}
      </ul>

      {opError && (
        <p className="text-destructive mt-2 text-sm" role="alert">
          {opError.status === 409
            ? resolve409Message(opError)
            : "Resolve failed. The Trash keeps any moved copies; refresh and retry."}
        </p>
      )}

      <div className="mt-3 flex items-center justify-end">
        <Button
          variant="destructive"
          disabled={removeIds.length === 0 || resolve.isPending}
          onClick={() => setConfirmOpen(true)}
        >
          {resolve.isPending ? (
            <>
              <Spinner className="animate-spin" aria-hidden="true" />
              Resolving&hellip;
            </>
          ) : (
            `Keep selected, move ${removeIds.length} to Trash`
          )}
        </Button>
      </div>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Move {removeIds.length} {removeIds.length === 1 ? "copy" : "copies"} to
              Trash?
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="text-sm">
                Keeping <strong>{keeper?.title}</strong>. These move to the Trash
                folder (reversible; nothing is deleted):
                <ul className="mt-2 list-disc pl-5">
                  {group.members
                    .filter((m) => m.id !== keeperId)
                    .map((m) => (
                      <li key={m.id} className="font-mono text-xs">
                        {m.folder}
                      </li>
                    ))}
                </ul>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={onConfirm}>Move to Trash</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/** One group member: a leading native keeper radio beside the shared AlbumRow
 * (AlbumRow has no selection slot — the radio is a sibling cell in this row
 * layout), with the folder path on its own full-width scroller line BELOW so
 * the distinguishing aunique `[NN]` suffix at the END of the path stays
 * reachable (an end-ellipsis would hide it; `title` still carries the whole
 * path for hover). */
function MemberRow({
  album,
  name,
  checked,
  onChoose,
}: Readonly<{
  album: DuplicateAlbum;
  name: string;
  checked: boolean;
  onChoose: () => void;
}> ) {
  const bitrateNote = ` · ${album.bitrate_kbps}k`;
  const quality = `${album.format ?? "-"}${album.bitrate_kbps ? bitrateNote : ""}`;
  return (
    // The radio centres on the ROW it selects, never on row+path. An
    // `items-center` flex <li> holding both centred the control on the whole
    // box and left it 17px (320-768) / 12px (1280) below its own row — the
    // same drift, from the same cause, as the bank row's checkbox in
    // BankSection.
    // A grid rather than that fix's stacked flex wrappers because the inset
    // the path line has to clear is the NATIVE radio's width (13px in
    // Chromium), which is the UA's number and not ours: placing the path in
    // row 2 of the SAME column track derives that offset instead of restating
    // it as a padding. `gap-x` only — a row gap would push the path off its
    // row, and `items-center` centres each item in its own grid row.
    <li
      className={cn(
        "grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-1",
        checked && "bg-primary/5",
      )}
    >
      {/* The ≥24px tap target (decisions 40) is a wrapping <label>, not a
          pseudo-element on the input: Chromium does render `::before` on an
          `<input>` and Firefox does not, while a label's whole box activates
          the control it wraps in every engine. `-m-3 p-3` leaves the label's
          MARGIN box equal to the input's own, so the grid column — and the
          path line that derives its inset from it — do not move; measured
          45×37 of target around a 13×13 drawn radio (the UA's box, which is
          why the target is a padding and not an inset of it).
          `relative` is load-bearing: without it the 12px that reach past the
          margin box are painted over by AlbumRow, the LATER in-flow sibling,
          and the right half of the target is 10.5px instead of 12. What it
          reaches into is AlbumRow's own `px-4`, 8px short of the cover. */}
      <label className="relative -m-3 flex p-3">
        <input
          type="radio"
          className="ml-2 shrink-0"
          name={name}
          checked={checked}
          onChange={onChoose}
          // The quality is IN the name, not only in the meta line beside it:
          // members of a group are duplicates of one album, so title and track
          // count are the same on every option and the name alone ("Keep In
          // Rainbows (10 tracks)") named all of them identically. `quality` is
          // the same string the row renders — one spelling, not a second.
          // It does not make the name unique when two members share a format
          // AND a bitrate; the only always-distinct field is the folder path,
          // and that is a design call, not this fix.
          aria-label={`Keep ${album.title} (${album.track_count} tracks, ${quality})`}
        />
      </label>
      {/* ?size=thumb: AlbumRow renders the cover at size-10 (40 CSS px), so
          the 320px derivation already covers 2x DPI. CoverArt never appends
          a query of its own, so a literal append is safe. */}
      <AlbumRow
        cover={`/api/albums/${album.id}/cover?size=thumb`}
        coverAssetKey={`album:${album.id}`}
        title={album.title}
        subtitle={album.album_artist}
        meta={`${album.year ?? "-"} · ${album.track_count} tracks · ${quality}`}
        badge={
          album.is_suggested_keeper ? (
            <Badge variant="secondary" className="shrink-0">
              <Resolved className="mr-1 size-3" aria-hidden="true" />
              most complete
            </Badge>
          ) : undefined
        }
      />
      <div
        className="text-muted-foreground thin-scrollbar col-start-2 overflow-x-auto px-4 pb-2 font-mono text-xs whitespace-nowrap"
        title={album.folder}
      >
        {album.folder}
      </div>
    </li>
  );
}
