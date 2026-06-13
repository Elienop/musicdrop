import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { useActiveImport } from "@/api/useActiveImport";
import type { BankDecision, BankItem, ExistingAlbum } from "@/api/useBank";
import {
  BankConflictError,
  useBankDecision,
  useBankDuplicates,
  useBankItem,
  useDeleteBankItem,
} from "@/api/useBank";
import type { DuplicateAction } from "@/api/useImport";
import {
  ImportConflictError,
  ImportStartRejectedError,
  useStartImport,
} from "@/api/useImport";
import { BackLink } from "@/components/albums/album-grid";
import { CandidateReview } from "@/components/import/CandidateReview";
import {
  DuplicateActions,
  DuplicateComparison,
} from "@/components/import/DuplicateReview";
import { Info, Remove, Spinner, Success, Warning } from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
import { PageSkeleton } from "@/components/system/PageSkeleton";
import { SectionLabel } from "@/components/system/SectionLabel";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useDeferredH1Focus } from "@/lib/useDeferredH1Focus";

/**
 * Review one BANKED row (`/review/bank/:itemId`) — the sweep already paid the
 * MusicBrainz lookups, so this renders instantly from the row's parked
 * payload via the same components as the live screens; decisions post to
 * `/api/bank/{id}/decision` (a store write — it never touches the import
 * slot; the apply runner moves files later) and return to Review, where the
 * row shows as queued. Branches by status first (queued/applying/done/
 * ignored/stale are notices, not decision screens), then by payload
 * (candidate · duplicate · no-match).
 */
export function BankReviewPage() {
  const { itemId } = useParams<{ itemId: string }>();
  const { data, isPending, isError } = useBankItem(itemId);
  useDeferredH1Focus(!isPending && !isError);

  if (isPending) {
    return (
      <Shell>
        <PageSkeleton announce="Loading the banked album…">
          <div className="flex flex-col gap-6">
            <Skeleton className="h-7 w-2/3" />
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Skeleton className="h-64 rounded-xl" />
              <Skeleton className="h-64 rounded-xl" />
            </div>
          </div>
        </PageSkeleton>
      </Shell>
    );
  }
  if (isError || !data) {
    // 404 = removed, applied away, or a dead deep link — return to the list.
    return (
      <Shell>
        <EmptyState
          bordered
          icon={Info}
          title="This row is no longer in the bank"
          body="It may have been applied, ignored, or removed. Head back to see what's waiting."
          action={
            <Button variant="outline" size="sm" asChild>
              <Link to="/review">Back to Review</Link>
            </Button>
          }
        />
      </Shell>
    );
  }
  return (
    <Shell>
      <BankScreen item={data} />
    </Shell>
  );
}

/** Page chrome: bank rows are always entered from Review — fixed up-link. */
function Shell({ children }: { children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-6" aria-label="Review banked album">
      <BackLink to="/review" label="Review" />
      {children}
    </section>
  );
}

function BankScreen({ item }: { item: BankItem }) {
  // Status first: rows the user cannot (or must not) decide render notices.
  if (item.status === "queued" || item.status === "applying") {
    return <PendingNotice item={item} />;
  }
  if (item.status === "done") {
    return <DoneNotice item={item} />;
  }
  if (item.status === "ignored") {
    return <IgnoredNotice item={item} />;
  }
  if (item.status === "stale") {
    return <StaleScreen item={item} />;
  }
  // needs_review | failed → a real decision screen, picked by payload.
  if (item.duplicate) {
    return <BankDuplicateScreen item={item} />;
  }
  if (item.parked) {
    return <BankCandidateScreen item={item} />;
  }
  return <NoMatchScreen item={item} />;
}

/** Shared decision error line — surfaces the backend's transition reason
 * (BankConflictError carries the string detail) or a generic retry cue. */
function DecisionError({ error }: { error: unknown }) {
  if (!error) return null;
  const message =
    error instanceof BankConflictError
      ? error.message
      : "Couldn’t submit that decision — try again.";
  return (
    <p className="text-destructive text-sm" role="alert">
      {message}
    </p>
  );
}

/** The failed-apply banner. role=alert via StatusBanner's destructive tone. */
function FailedBanner({ error }: { error: string | null | undefined }) {
  return (
    <StatusBanner tone="destructive" icon={Warning}>
      <p className="font-medium">The apply failed — decide again to retry.</p>
      {error && <p className="text-muted-foreground text-sm">{error}</p>}
    </StatusBanner>
  );
}

/** Duplicate-resolution strip for FAILED rows. The apply runner fails a row
 * that meets an unanticipated library duplicate with "decide again with a
 * duplicate action" — `{action: "duplicate"}` is accepted on any decidable
 * row, so the strip is offered on every failed row (harmless otherwise)
 * rather than sniffing the error string. */
function FailedDuplicateStrip({
  busy,
  pending,
  onDecide,
}: {
  busy: boolean;
  pending: DuplicateAction | null;
  onDecide: (action: DuplicateAction) => void;
}) {
  return (
    <section aria-label="Resolve as a duplicate" className="flex flex-col gap-3">
      <SectionLabel>Resolve as a duplicate</SectionLabel>
      <p className="text-muted-foreground text-sm">
        Use these if the apply failed because the album is already in your
        library.
      </p>
      <DuplicateActions pending={pending} busy={busy} onDecide={onDecide} />
    </section>
  );
}

function BankCandidateScreen({ item }: { item: BankItem }) {
  const navigate = useNavigate();
  const decide = useBankDecision(item.id);
  const [selected, setSelected] = useState(0);
  const [pendingDup, setPendingDup] = useState<DuplicateAction | null>(null);
  // The BankScreen branch guarantees parked; narrow for tsc without a cast.
  const parked = item.parked;
  // A candidate row is decidable while it awaits a verdict (fresh) OR after a
  // failed apply (retry) — both run the up-front library-collision check keyed
  // on the selected release; queued/applying/done rows never reach here.
  const decidable = item.status === "needs_review" || item.status === "failed";
  const dups = useBankDuplicates(item.id, selected, decidable && parked != null);
  if (!parked) return null;

  // The matched release already exists in the library — fold beets' four
  // duplicate actions into the footer so one click both pins this release and
  // resolves the collision (no failed apply, no re-open). While the check is
  // pending or finds nothing, the normal footer stands.
  const existing = dups.data?.existing ?? [];
  const hasCollision = existing.length > 0;

  const submit = (decision: BankDecision) =>
    decide.mutate(decision, {
      onSuccess: () => navigate("/review"),
      onError: () => setPendingDup(null),
    });

  return (
    <div className="flex flex-col gap-6">
      {item.status === "failed" && <FailedBanner error={item.error} />}
      <CandidateReview
        candidate={parked.candidate}
        // Banked rows keep metadata only — the live job's current-art
        // endpoint died with the sweep. The placeholder is the honest state;
        // the after-panel's Cover Art Archive URL still renders.
        nowCoverUrl={null}
        selected={selected}
        onSelect={setSelected}
      />
      {hasCollision ? (
        <>
          <AlreadyInLibraryNotice existing={existing} />
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={decide.isPending}
              onClick={() => submit({ action: "ignore" })}
            >
              Ignore
            </Button>
            <DecisionError error={decide.error} />
          </div>
          <DuplicateActions
            pending={pendingDup}
            busy={decide.isPending}
            onDecide={(action) => {
              setPendingDup(action);
              submit({
                action: "duplicate",
                candidate_index: selected,
                duplicate_action: action,
              });
            }}
          />
        </>
      ) : (
        <div className="bg-background/80 sticky bottom-0 z-10 -mx-2 flex flex-col gap-1.5 border-t px-2 py-3 backdrop-blur">
          {selected !== 0 && (
            <p className="text-muted-foreground text-sm" role="status">
              Showing the top match — Apply will queue the selected release.
            </p>
          )}
          <DecisionError error={decide.error} />
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={decide.isPending}
              onClick={() => submit({ action: "ignore" })}
            >
              Ignore
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={decide.isPending}
              aria-describedby="bank-actions-hint"
              onClick={() => submit({ action: "asis" })}
            >
              Use as-is
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={decide.isPending}
              aria-describedby="bank-actions-hint"
              onClick={() => submit({ action: "astracks" })}
            >
              As tracks
            </Button>
            <Button
              className="ml-auto"
              disabled={decide.isPending}
              onClick={() => submit({ action: "apply", candidate_index: selected })}
            >
              {decide.isPending ? (
                <>
                  <Spinner className="animate-spin" aria-hidden="true" /> Queuing…
                </>
              ) : (
                <>
                  <Success aria-hidden="true" /> Apply
                </>
              )}
            </Button>
          </div>
          <p id="bank-actions-hint" className="text-muted-foreground text-xs">
            Decisions queue for the background apply — files move when the
            import slot is free. Use as-is imports with your current tags; As
            tracks imports each file as a standalone track.
          </p>
        </div>
      )}
    </div>
  );
}

/** The up-front "this already exists" notice on a banked candidate screen —
 * lists each colliding library copy with a View link, above the four duplicate
 * actions. The candidate switcher stays live, so Keep both / Replace / Merge
 * tag the new copy as the SELECTED release. */
function AlreadyInLibraryNotice({ existing }: { existing: ExistingAlbum[] }) {
  return (
    <section aria-label="Already in your library" className="flex flex-col gap-3">
      <SectionLabel>Already in your library</SectionLabel>
      <p className="text-muted-foreground text-sm">
        This album matches{" "}
        {existing.length === 1 ? "one you already have" : `${existing.length} you already have`}.
        Choose what to do below — your choice imports the selected release.
      </p>
      <ul className="flex flex-col gap-2">
        {existing.map((album) => (
          <li
            key={album.album_id}
            className="border-border flex items-center justify-between gap-3 rounded-lg border p-3"
          >
            <span className="truncate text-sm">
              <span className="font-medium">{album.album_artist ?? "Unknown artist"}</span>
              {" — "}
              {album.album ?? "Unknown album"}
            </span>
            <Button variant="outline" size="sm" asChild>
              <Link to={`/albums/${album.album_id}`}>View</Link>
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function BankDuplicateScreen({ item }: { item: BankItem }) {
  const navigate = useNavigate();
  const decide = useBankDecision(item.id);
  const [pending, setPending] = useState<DuplicateAction | null>(null);
  const prompt = item.duplicate;
  if (!prompt) return null;

  function onDecide(action: DuplicateAction) {
    setPending(action);
    decide.mutate(
      { action: "duplicate", duplicate_action: action },
      { onSuccess: () => navigate("/review"), onError: () => setPending(null) },
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {item.status === "failed" && <FailedBanner error={item.error} />}
      <DuplicateComparison prompt={prompt} incomingCoverUrl={null} />
      <div className="flex items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          disabled={decide.isPending}
          onClick={() =>
            decide.mutate({ action: "ignore" }, { onSuccess: () => navigate("/review") })
          }
        >
          Ignore
        </Button>
        <DecisionError error={decide.error} />
      </div>
      <DuplicateActions pending={pending} busy={decide.isPending} onDecide={onDecide} />
    </div>
  );
}

function NoMatchScreen({ item }: { item: BankItem }) {
  const navigate = useNavigate();
  const decide = useBankDecision(item.id);
  const [pendingDup, setPendingDup] = useState<DuplicateAction | null>(null);
  const submit = (decision: BankDecision) =>
    decide.mutate(decision, {
      onSuccess: () => navigate("/review"),
      onError: () => setPendingDup(null),
    });

  return (
    <div className="flex flex-col gap-6">
      {item.status === "failed" && <FailedBanner error={item.error} />}
      <div className="flex flex-col gap-1">
        <h1 tabIndex={-1} className="font-display text-display font-semibold tracking-tight">
          No match found
        </h1>
        <p className="text-muted-foreground text-sm">
          beets couldn&rsquo;t match this folder against MusicBrainz. Import it
          with its current tags, as standalone tracks, or ignore it.
        </p>
      </div>
      <p className="text-muted-foreground font-mono text-xs" title={item.folder}>
        {item.folder}
      </p>
      {item.status === "failed" && (
        <FailedDuplicateStrip
          busy={decide.isPending}
          pending={pendingDup}
          onDecide={(action) => {
            setPendingDup(action);
            submit({ action: "duplicate", duplicate_action: action });
          }}
        />
      )}
      <DecisionError error={decide.error} />
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" disabled={decide.isPending} onClick={() => submit({ action: "ignore" })}>
          Ignore
        </Button>
        <Button variant="outline" size="sm" disabled={decide.isPending} onClick={() => submit({ action: "astracks" })}>
          As tracks
        </Button>
        <Button className="ml-auto" disabled={decide.isPending} onClick={() => submit({ action: "asis" })}>
          {decide.isPending ? (
            <>
              <Spinner className="animate-spin" aria-hidden="true" /> Queuing…
            </>
          ) : (
            "Use as-is"
          )}
        </Button>
      </div>
    </div>
  );
}

/** queued/applying: the apply runner owns the row; the hook polls (2s) so
 * this screen progresses to done/failed live. No actions — deciding 409s and
 * deleting an applying row 409s. */
function PendingNotice({ item }: { item: BankItem }) {
  const applying = item.status === "applying";
  return (
    <EmptyState
      bordered
      icon={Info}
      title={applying ? "Applying now" : "Queued to apply"}
      body={
        applying
          ? "beets is importing this folder — this page updates when it lands."
          : "This decision waits for the import slot. Files move automatically; no further action needed."
      }
    />
  );
}

function DoneNotice({ item }: { item: BankItem }) {
  return (
    <EmptyState
      bordered
      icon={Success}
      title="Imported"
      body={`${item.artist ?? "Unknown artist"} — ${item.album ?? lastSegment(item.folder)} landed in your library.`}
      action={
        item.album_id != null ? (
          <Button size="sm" asChild>
            <Link to={`/albums/${item.album_id}`}>View album</Link>
          </Button>
        ) : (
          <RemoveRowButton itemId={item.id} />
        )
      }
    />
  );
}

function IgnoredNotice({ item }: { item: BankItem }) {
  return (
    <EmptyState
      bordered
      icon={Info}
      title="Ignored"
      body="This folder was left as-is. Remove the row to clear it from the list — the files are untouched."
      action={<RemoveRowButton itemId={item.id} />}
    />
  );
}

/** Remove-the-row action shared by the settled notices. */
function RemoveRowButton({ itemId }: { itemId: string }) {
  const navigate = useNavigate();
  const remove = useDeleteBankItem();
  return (
    <Button
      variant="outline"
      size="sm"
      disabled={remove.isPending}
      onClick={() => remove.mutate(itemId, { onSuccess: () => navigate("/review") })}
    >
      <Remove aria-hidden="true" /> Remove from bank
    </Button>
  );
}

/**
 * stale: the folder changed (or vanished) after banking — the parked diff no
 * longer describes reality, and a re-posted decision would round-trip back to
 * stale by design (the apply runner re-verifies the fingerprint). The ONLY
 * forward paths are a fresh ATTENDED import of the folder or removing the
 * row. "Review now" posts the row's server-side folder back to
 * `POST /api/import` — the same operator-supplied-path trust model as the
 * manual start screen — then deletes the row (best-effort: the attended
 * import owns the folder from this moment, and a re-sweep can never re-bank
 * it — beets' taghistory excludes it) and navigates into the job. The button
 * needs the import slot, so it gates on the active probe with the visible
 * reason below (never a disabled-button title).
 */
function StaleScreen({ item }: { item: BankItem }) {
  const navigate = useNavigate();
  const start = useStartImport();
  const remove = useDeleteBankItem();
  const active = useActiveImport();
  const importActive = active.data?.active ?? false;
  const busy = start.isPending || remove.isPending;

  function reviewNow() {
    start.mutate(
      { path: item.folder },
      {
        onSuccess: async (res) => {
          // Best-effort tombstone cleanup — a failed delete leaves a row the
          // user can remove manually; it must never block the started import.
          await remove.mutateAsync(item.id).catch(() => undefined);
          navigate(`/import?job=${res.job_id}`);
        },
      },
    );
  }

  const startError =
    start.error instanceof ImportConflictError
      ? "An import is already running — try again when it finishes."
      : start.error instanceof ImportStartRejectedError
        ? start.error.message
        : start.isError
          ? "Couldn’t start the re-scan. Check the backend, then try again."
          : null;

  return (
    <div className="flex flex-col gap-6">
      <StatusBanner tone="warning" icon={Warning}>
        <p className="font-medium">This folder changed after it was banked.</p>
        <p className="text-muted-foreground text-sm">
          {item.error ?? "The banked candidates no longer match the files."} Re-scan
          it to review fresh matches — the banked ones are out of date.
        </p>
      </StatusBanner>
      <p className="text-muted-foreground font-mono text-xs" title={item.folder}>
        {item.folder}
      </p>
      {startError && (
        <p className="text-destructive text-sm" role="alert">
          {startError}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => remove.mutate(item.id, { onSuccess: () => navigate("/review") })}
        >
          <Remove aria-hidden="true" /> Remove from bank
        </Button>
        <Button
          className="ml-auto"
          disabled={busy || importActive}
          aria-describedby={importActive ? "stale-rescan-hint" : undefined}
          onClick={reviewNow}
        >
          {start.isPending ? (
            <>
              <Spinner className="animate-spin" aria-hidden="true" /> Starting…
            </>
          ) : (
            "Review now"
          )}
        </Button>
      </div>
      {importActive && (
        <p id="stale-rescan-hint" className="text-muted-foreground text-xs">
          An import is already running — the re-scan needs the import slot.
          Decisions on other banked rows still work meanwhile.
        </p>
      )}
    </div>
  );
}

/** Last path segment, for rows without a parsed album title. */
function lastSegment(folder: string): string {
  const parts = folder.split("/").filter(Boolean);
  return parts.at(-1) ?? folder;
}
