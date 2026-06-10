import { useQueryClient } from "@tanstack/react-query";
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
import { cn } from "@/lib/utils";

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
        onError: (err) => {
          if ((err as DuplicatesOpError).status === 409) {
            void queryClient.invalidateQueries({ queryKey: ["duplicates"] });
          }
        },
      },
    );
  }

  const allError = resolveAll.error as DuplicatesOpError | null;
  const skipped = summary?.skipped_stale.length ?? 0;

  return (
    <PageBody>
      <PageHeader
        title="Duplicates"
        meta={
          data
            ? `${data.group_count} ${data.group_count === 1 ? "group" : "groups"} · ${data.album_count} albums`
            : "Scanning your library…"
        }
        actions={
          <>
            {showBulk && (
              <Button
                variant="destructive"
                disabled={resolveAll.isPending}
                onClick={() => setConfirmAllOpen(true)}
              >
                {resolveAll.isPending ? (
                  <>
                    <Spinner className="animate-spin" aria-hidden="true" />
                    Resolving&hellip;
                  </>
                ) : (
                  `Resolve all · ${moveCount} ${moveCount === 1 ? "copy" : "copies"}`
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
                // Switching modes regroups the library — drop keeper overrides
                // (a same keeper-id can recur with different membership across
                // modes) and the stale summary so the bulk action can't carry a
                // wrong choice over.
                const m: DuplicateMode = v === "fuzzy" ? "fuzzy" : "strict";
                setMode(m);
                setSelections({});
                setSummary(null);
              }}
            />
          </>
        }
      />

      {summary &&
        (summary.moved_count > 0 ? (
          <p className="text-muted-foreground text-sm" role="status">
            Moved {summary.moved_count} {summary.moved_count === 1 ? "copy" : "copies"} across{" "}
            {summary.group_count} {summary.group_count === 1 ? "group" : "groups"} to Trash.
            {skipped > 0 &&
              ` ${skipped} group${skipped === 1 ? "" : "s"} changed and ${skipped === 1 ? "was" : "were"} skipped — refreshed; re-check ${skipped === 1 ? "it" : "them"}.`}
          </p>
        ) : (
          // All groups drifted since the scan (a normal 200 with nothing moved):
          // lead with the actionable part, not a "moved 0" that reads as a no-op.
          <p className="text-sm" role="status">
            Nothing moved — {skipped === 1 ? "the group" : `all ${skipped} groups`} changed
            since the scan and {skipped === 1 ? "was" : "were"} skipped. The report refreshed;
            re-check {skipped === 1 ? "it" : "them"}.
          </p>
        ))}
      {allError && (
        <p className="text-destructive text-sm" role="alert">
          {allError.status === 409
            ? resolve409Message(allError)
            : "Resolve all failed. The Trash keeps any moved copies; refresh and retry."}
        </p>
      )}

      {isPending && (
        <p className="text-muted-foreground flex items-center gap-2 text-sm" role="status">
          <Spinner className="size-3.5 animate-spin" aria-hidden="true" />
          Scanning library for duplicates&hellip;
        </p>
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

      <AlertDialog open={confirmAllOpen} onOpenChange={setConfirmAllOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Move {moveCount} {moveCount === 1 ? "copy" : "copies"} across{" "}
              {data?.group_count ?? 0} {(data?.group_count ?? 0) === 1 ? "group" : "groups"} to
              Trash?
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="text-sm">
                Keeping one copy per group (the marked keeper). These move to the
                Trash folder (reversible — nothing is deleted):
                <ul className="mt-2 max-h-64 space-y-1 overflow-y-auto">
                  {decisions.map((d) => {
                    const keeper = d.group.members.find((m) => m.id === d.keep);
                    return (
                      <li key={d.group.suggested_keeper_id} className="text-xs">
                        Keep <strong>{keeper?.title}</strong>
                        <span className="text-muted-foreground">
                          {" "}
                          — {keeper?.album_artist} · move {d.removeIds.length}
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
            <AlertDialogAction onClick={onConfirmAll}>Move all to Trash</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageBody>
  );
}

/** Inline message for a 409 from resolve. Distinguishes the two backend causes
 * (import-active vs stale-group) from the flat `detail` string so the user gets
 * an actionable hint instead of one generic line. */
function resolve409Message(err: DuplicatesOpError): string {
  const detail = (err.body as { detail?: string } | null)?.detail ?? "";
  return detail.toLowerCase().includes("import")
    ? "Can't resolve while an import is running."
    : "This group changed — refreshing. Re-check the copies and retry.";
}

function GroupCard({
  group,
  mode,
  keeperId,
  onChoose,
}: {
  group: DuplicateGroup;
  mode: DuplicateMode;
  keeperId: number;
  onChoose: (albumId: number) => void;
}) {
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
        onError: (err) => {
          if ((err as DuplicatesOpError).status === 409) {
            void queryClient.invalidateQueries({ queryKey: ["duplicates"] });
          }
        },
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
                folder (reversible — nothing is deleted):
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
}: {
  album: DuplicateAlbum;
  name: string;
  checked: boolean;
  onChoose: () => void;
}) {
  const quality = `${album.format ?? "—"}${album.bitrate_kbps ? ` · ${album.bitrate_kbps}k` : ""}`;
  return (
    <li className={cn("flex items-center gap-1", checked && "bg-primary/5")}>
      <input
        type="radio"
        className="ml-2 shrink-0"
        name={name}
        checked={checked}
        onChange={onChoose}
        aria-label={`Keep ${album.title} (${album.track_count} tracks)`}
      />
      <div className="min-w-0 flex-1">
        <AlbumRow
          cover={`/api/albums/${album.id}/cover`}
          title={album.title}
          subtitle={album.album_artist}
          meta={`${album.year ?? "—"} · ${album.track_count} tracks · ${quality}`}
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
          className="text-muted-foreground thin-scrollbar overflow-x-auto px-4 pb-2 font-mono text-xs whitespace-nowrap"
          title={album.folder}
        >
          {album.folder}
        </div>
      </div>
    </li>
  );
}
