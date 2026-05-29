import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Music, ShieldCheck } from "lucide-react";
import { useState } from "react";

import {
  type DuplicateAlbum,
  type DuplicateGroup,
  type DuplicateMode,
  type DuplicatesOpError,
  useDuplicates,
  useResolveDuplicate,
} from "@/api/useDuplicates";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

export function DuplicatesPage() {
  const [mode, setMode] = useState<DuplicateMode>("strict");
  const { data, isPending, isError, refetch } = useDuplicates(mode);

  return (
    <section className="flex flex-col gap-4" aria-label="Duplicate albums">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2 className="text-2xl font-semibold tracking-tight">Duplicate albums</h2>
          <p className="text-muted-foreground text-sm">
            {data
              ? `${data.group_count} ${data.group_count === 1 ? "group" : "groups"} · ${data.album_count} albums`
              : "Scanning your library…"}
          </p>
        </div>
        <ModeToggle mode={mode} onChange={setMode} />
      </header>

      {isPending && <LoadingState />}
      {isError && <ErrorState onRetry={() => void refetch()} />}
      {data && data.groups.length === 0 && <EmptyState />}
      {data &&
        data.groups.map((group) => (
          <GroupCard key={group.suggested_keeper_id} group={group} mode={mode} />
        ))}
    </section>
  );
}

function ModeToggle({
  mode,
  onChange,
}: {
  mode: DuplicateMode;
  onChange: (m: DuplicateMode) => void;
}) {
  return (
    <div
      className="inline-flex overflow-hidden rounded-lg border text-sm"
      role="group"
      aria-label="Match mode"
    >
      {(["strict", "fuzzy"] as const).map((m) => (
        <button
          key={m}
          type="button"
          onClick={() => onChange(m)}
          aria-pressed={mode === m}
          className={cn(
            "px-3 py-1.5 font-medium transition-colors",
            mode === m
              ? "bg-foreground text-background"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {m === "strict" ? "Strict · MB-ID" : "Fuzzy · artist + title"}
        </button>
      ))}
    </div>
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

function GroupCard({ group, mode }: { group: DuplicateGroup; mode: DuplicateMode }) {
  const [keeperId, setKeeperId] = useState(group.suggested_keeper_id);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const resolve = useResolveDuplicate();
  const queryClient = useQueryClient();

  // Clamp the keeper to current membership. A background refetch can change a
  // group's members while keeping suggested_keeper_id (so the card keeps its
  // React key and does NOT remount), which would leave keeperId pointing at an
  // album no longer present — making removeIds cover the whole group and the
  // dialog read "Keeping <nothing>". Falling back to the suggested keeper keeps
  // the confirm dialog + remove_album_ids truthful.
  const effectiveKeeperId = group.members.some((m) => m.id === keeperId)
    ? keeperId
    : group.suggested_keeper_id;
  const keeper = group.members.find((m) => m.id === effectiveKeeperId);
  const removeIds = group.members
    .filter((m) => m.id !== effectiveKeeperId)
    .map((m) => m.id);

  function onConfirm() {
    resolve.mutate(
      { mode, keep_album_id: effectiveKeeperId, remove_album_ids: removeIds },
      {
        onSettled: () => setConfirmOpen(false),
        onError: (err) => {
          // 409 = stale group (membership changed) or an import is running.
          // Refetch so the report self-heals to the current library state
          // instead of leaving the now-wrong group on screen.
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
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-10">Keep</TableHead>
            <TableHead>Album</TableHead>
            <TableHead className="w-16">Year</TableHead>
            <TableHead className="w-16">Tracks</TableHead>
            <TableHead className="w-28">Quality</TableHead>
            <TableHead>Folder</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {group.members.map((album) => (
            <MemberRow
              key={album.id}
              album={album}
              // Shared per-GROUP radio name so the copies form one radio group
              // (single-select + arrow-key nav). Stable across re-render.
              name={`keeper-${group.suggested_keeper_id}`}
              checked={album.id === effectiveKeeperId}
              onChoose={() => setKeeperId(album.id)}
            />
          ))}
        </TableBody>
      </Table>

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
              <Loader2 className="animate-spin" aria-hidden="true" />
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
                    .filter((m) => m.id !== effectiveKeeperId)
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
  return (
    <TableRow className={cn(checked && "bg-primary/5")}>
      <TableCell>
        <input
          type="radio"
          name={name}
          checked={checked}
          onChange={onChoose}
          aria-label={`Keep ${album.title} (${album.track_count} tracks)`}
        />
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-2">
          <Thumb album={album} />
          <div className="min-w-0">
            <div className="truncate font-medium" title={album.title}>
              {album.title}
              {album.is_suggested_keeper && (
                <Badge variant="secondary" className="ml-2 align-middle">
                  <ShieldCheck className="mr-1 size-3" aria-hidden="true" />
                  most complete
                </Badge>
              )}
            </div>
            <div className="text-muted-foreground truncate text-xs">{album.album_artist}</div>
          </div>
        </div>
      </TableCell>
      <TableCell>{album.year ?? "—"}</TableCell>
      <TableCell>{album.track_count}</TableCell>
      <TableCell className="text-sm">
        {album.format ?? "—"}
        {album.bitrate_kbps ? ` · ${album.bitrate_kbps}k` : ""}
      </TableCell>
      <TableCell className="max-w-xs">
        {/* Horizontally scrollable so the full path is reachable without
            truncation — the distinguishing aunique `[NN]` suffix lives at the
            END, which an end-ellipsis would hide. whitespace-nowrap keeps it on
            one line; overflow-x-auto adds a scrollbar only when it overflows.
            The title= still carries the whole path for a hover tooltip. */}
        <div
          className="text-muted-foreground thin-scrollbar overflow-x-auto font-mono text-xs whitespace-nowrap"
          title={album.folder}
        >
          {album.folder}
        </div>
      </TableCell>
    </TableRow>
  );
}

/** Small album cover thumbnail backed by GET /api/albums/{id}/cover, falling
 * back to a music-note placeholder on 404/decode error (same idiom as the
 * album grid's CoverImage). */
function Thumb({ album }: { album: DuplicateAlbum }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className="bg-muted flex size-9 items-center justify-center rounded" role="img" aria-label="No cover">
        <Music className="text-muted-foreground size-4" aria-hidden="true" />
      </div>
    );
  }
  return (
    <img
      src={`/api/albums/${album.id}/cover`}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
      className="bg-muted size-9 rounded object-cover"
    />
  );
}

function LoadingState() {
  return (
    <p className="text-muted-foreground flex items-center gap-2 text-sm" role="status">
      <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
      Scanning library for duplicates&hellip;
    </p>
  );
}

function EmptyState() {
  return (
    <div className="rounded-xl border border-dashed p-8 text-center" role="status">
      <ShieldCheck className="text-muted-foreground mx-auto mb-2 size-8" aria-hidden="true" />
      <p className="font-medium">No duplicate albums found</p>
      <p className="text-muted-foreground text-sm">Your library is clean in this mode.</p>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="border-destructive/40 bg-destructive/5 flex items-center justify-between gap-3 rounded-xl border p-4" role="alert">
      <p className="text-sm">Couldn't load duplicates.</p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
