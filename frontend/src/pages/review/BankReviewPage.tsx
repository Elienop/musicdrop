import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { useActiveImport } from "@/api/useActiveImport";
import type { BankDecision, BankItem } from "@/api/useBank";
import {
  BankConflictError,
  useBankDecision,
  useBankDuplicates,
  useBankItem,
  useBankRescan,
  useBankSearch,
  useDeleteBankItem,
} from "@/api/useBank";
import type { DuplicateAction } from "@/api/useImport";
import {
  ImportConflictError,
  ImportStartRejectedError,
  useStartImport,
} from "@/api/useImport";
import { BackLink } from "@/components/albums/album-grid";
import { AlreadyInLibrary } from "@/components/import/AlreadyInLibrary";
import { CandidateReview } from "@/components/import/CandidateReview";
import {
  DuplicateActions,
  DuplicateComparison,
} from "@/components/import/DuplicateReview";
import { ReleaseSearchPanel } from "@/components/import/ReleaseSearchPanel";
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

/** The standalone Ignore + decision-error strip shared by the bank screens
 * whose Ignore action sits on its own row (the candidate-collision fold and
 * the duplicate screen). Screens that interleave Ignore with other actions in
 * a single flex row (no-match, the candidate apply footer) keep their own
 * markup — their `ml-auto` pin needs every button in one container. Each
 * caller passes its own busy expression and ignore handler. */
function BankDecisionFooter({
  busy,
  onIgnore,
  error,
}: {
  busy: boolean;
  onIgnore: () => void;
  error: unknown;
}) {
  return (
    <div className="flex items-center gap-2">
      <Button variant="ghost" size="sm" disabled={busy} onClick={onIgnore}>
        Ignore
      </Button>
      <DecisionError error={error} />
    </div>
  );
}

/** The search's conflict line — a 409's real detail (stale flip, status race).
 * Non-conflict transport errors render inside the panel instead. */
function SearchConflict({ error }: { error: unknown }) {
  if (!(error instanceof BankConflictError)) return null;
  return (
    <p className="text-destructive text-sm" role="alert">
      {error.message}
    </p>
  );
}

/** "Rescan folder" — the explicit escape when the user changed the files on
 * disk (deleted a duplicate track, restored one). Re-reads + re-matches +
 * refreshes the fingerprint; the cache write re-branches this page to the
 * row's new state. The hook is hoisted to each screen so its pending state
 * can busy-gate the screen's other actions. */
function RescanControl({
  rescan,
  disabled,
  hint = false,
  variant = "outline",
}: {
  rescan: ReturnType<typeof useBankRescan>;
  disabled: boolean;
  hint?: boolean;
  variant?: "outline" | "default";
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-3">
        <Button
          variant={variant}
          size="sm"
          disabled={disabled || rescan.isPending}
          onClick={() => rescan.mutate()}
        >
          {rescan.isPending ? (
            <>
              <Spinner className="animate-spin" aria-hidden="true" /> Rescanning…
            </>
          ) : (
            "Rescan folder"
          )}
        </Button>
        {hint && (
          <p className="text-muted-foreground text-xs">
            Re-reads the folder from disk and matches it again.
          </p>
        )}
      </div>
      <SearchConflict error={rescan.error} />
      {rescan.isError && !(rescan.error instanceof BankConflictError) && (
        <p className="text-destructive text-sm" role="alert">
          Couldn’t rescan the folder — try again.
        </p>
      )}
    </div>
  );
}

const NO_HIT_FEEDBACK =
  "No release found for that search — showing your previous matches.";

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
      <DuplicateActions
        pending={pending}
        busy={busy}
        onDecide={onDecide}
        context="bank"
      />
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
  const search = useBankSearch(item.id);
  const rescan = useBankRescan(item.id);
  // A landed search replaces the payload — reset the switcher to the new top.
  useEffect(() => setSelected(0), [parked?.candidate.search_revision]);
  const dups = useBankDuplicates(item.id, selected, decidable && parked != null);
  if (!parked) return null;

  // The matched release already exists in the library — fold beets' four
  // duplicate actions into the footer so one click both pins this release and
  // resolves the collision (no failed apply, no re-open). While the check is
  // pending or finds nothing, the normal footer stands.
  const existing = dups.data?.existing ?? [];
  const hasCollision = existing.length > 0;
  // The first collision check is still in flight (the query is always enabled
  // on this decidable screen). Block the apply-style actions until it lands so
  // a fast click can't queue an apply that the runner would just fail with a
  // duplicate block. Ignore stays free; the layout doesn't shift (disable, not
  // unmount).
  const checking = dups.isLoading;
  // Offer the four duplicate actions on a real collision OR — as a fallback —
  // when the re-check ERRORS on an already-failed row, so the failed-banner's
  // "decide again with a duplicate action" instruction stays followable.
  const showDupActions = hasCollision || (dups.isError && item.status === "failed");

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
      {decidable && (
        <>
          <ReleaseSearchPanel
            onSearch={(s) => search.mutate(s)}
            busy={search.isPending || decide.isPending || rescan.isPending}
            feedback={search.data?.found === false ? NO_HIT_FEEDBACK : null}
            error={search.isError && !(search.error instanceof BankConflictError)}
          />
          <SearchConflict error={search.error} />
          <RescanControl
            rescan={rescan}
            disabled={decide.isPending || search.isPending}
          />
        </>
      )}
      {showDupActions ? (
        <>
          {hasCollision ? (
            <AlreadyInLibrary
              existing={existing}
              blurb={
                existing.length === 1
                  ? "This album matches one you already have. Choose what to do below — your choice imports the selected release."
                  : `This album matches ${existing.length} you already have. Choose what to do below — your choice imports the selected release.`
              }
            />
          ) : (
            // Error-on-failed fallback: the re-check couldn't run, so we can't
            // list the colliding copies, but the failed-banner already told the
            // user to resolve it as a duplicate — keep that path reachable.
            <section
              aria-label="Resolve as a duplicate"
              className="flex flex-col gap-2"
            >
              <SectionLabel>Resolve as a duplicate</SectionLabel>
              <p className="text-muted-foreground text-sm">
                Couldn’t re-check your library. If the apply failed because this
                album is already in it, resolve it as a duplicate below.
              </p>
            </section>
          )}
          <BankDecisionFooter
            busy={decide.isPending || search.isPending || rescan.isPending}
            onIgnore={() => submit({ action: "ignore" })}
            error={decide.error}
          />
          <DuplicateActions
            pending={pendingDup}
            busy={decide.isPending || search.isPending || rescan.isPending}
            context="bank"
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
          {checking && (
            <p className="text-muted-foreground text-sm" role="status">
              Checking your library…
            </p>
          )}
          <DecisionError error={decide.error} />
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={decide.isPending || search.isPending || rescan.isPending}
              onClick={() => submit({ action: "ignore" })}
            >
              Ignore
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={decide.isPending || checking || search.isPending || rescan.isPending}
              aria-describedby="bank-actions-hint"
              onClick={() => submit({ action: "asis" })}
            >
              Use as-is
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={decide.isPending || checking || search.isPending || rescan.isPending}
              aria-describedby="bank-actions-hint"
              onClick={() => submit({ action: "astracks" })}
            >
              As tracks
            </Button>
            <Button
              className="ml-auto"
              disabled={decide.isPending || checking || search.isPending || rescan.isPending}
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

function BankDuplicateScreen({ item }: { item: BankItem }) {
  const navigate = useNavigate();
  const decide = useBankDecision(item.id);
  const rescan = useBankRescan(item.id);
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
      <RescanControl rescan={rescan} disabled={decide.isPending} hint />
      <BankDecisionFooter
        busy={decide.isPending || rescan.isPending}
        onIgnore={() =>
          decide.mutate({ action: "ignore" }, { onSuccess: () => navigate("/review") })
        }
        error={decide.error}
      />
      <DuplicateActions
        pending={pending}
        busy={decide.isPending || rescan.isPending}
        onDecide={onDecide}
        context="bank"
      />
    </div>
  );
}

function NoMatchScreen({ item }: { item: BankItem }) {
  const navigate = useNavigate();
  const decide = useBankDecision(item.id);
  const search = useBankSearch(item.id);
  const rescan = useBankRescan(item.id);
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
          beets couldn&rsquo;t match this folder against MusicBrainz. Search for
          the right release below, import it with its current tags, as
          standalone tracks, or ignore it.
        </p>
      </div>
      <p className="text-muted-foreground font-mono text-xs" title={item.folder}>
        {item.folder}
      </p>
      <ReleaseSearchPanel
        onSearch={(s) => search.mutate(s)}
        busy={search.isPending || decide.isPending || rescan.isPending}
        feedback={search.data?.found === false ? NO_HIT_FEEDBACK : null}
        error={search.isError && !(search.error instanceof BankConflictError)}
      />
      <SearchConflict error={search.error} />
      <RescanControl
        rescan={rescan}
        disabled={decide.isPending || search.isPending}
        hint
      />
      {item.status === "failed" && (
        <FailedDuplicateStrip
          busy={decide.isPending || search.isPending || rescan.isPending}
          pending={pendingDup}
          onDecide={(action) => {
            setPendingDup(action);
            submit({ action: "duplicate", duplicate_action: action });
          }}
        />
      )}
      <DecisionError error={decide.error} />
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" disabled={decide.isPending || search.isPending || rescan.isPending} onClick={() => submit({ action: "ignore" })}>
          Ignore
        </Button>
        <Button variant="outline" size="sm" disabled={decide.isPending || search.isPending || rescan.isPending} onClick={() => submit({ action: "astracks" })}>
          As tracks
        </Button>
        <Button className="ml-auto" disabled={decide.isPending || search.isPending || rescan.isPending} onClick={() => submit({ action: "asis" })}>
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
  const { title, body } = doneOutcome(item);
  return (
    <EmptyState
      bordered
      icon={Success}
      title={title}
      body={body}
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

/** Title + body describing what the resolved row actually did, read from the
 * decision. A skip_new duplicate resolution KEPT your copy — it never "landed
 * in your library", so it must not claim it did. */
function doneOutcome(item: BankItem): { title: string; body: string } {
  const label = `${item.artist ?? "Unknown artist"} — ${item.album ?? lastSegment(item.folder)}`;
  const decided = item.decided;
  if (decided?.action === "duplicate") {
    switch (decided.duplicate_action) {
      case "skip_new":
        return {
          title: "Kept your existing copy",
          body: "Nothing new was imported — your existing copy is untouched.",
        };
      case "replace":
        return {
          title: "Replaced",
          body: `${label} was imported; the old copy was moved to Trash.`,
        };
      case "merge":
        return { title: "Merged", body: `${label} was combined into your library.` };
      // keep_both lands a new album — fall through to the "Imported" wording.
    }
  }
  return { title: "Imported", body: `${label} landed in your library.` };
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
  const rescan = useBankRescan(item.id);
  const active = useActiveImport();
  const importActive = active.data?.active ?? false;
  const busy = start.isPending || remove.isPending || rescan.isPending;

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
          {item.error ?? "The banked candidates no longer match the files."} Rescan
          it below to review fresh matches — the banked ones are out of date.
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
      <RescanControl
        rescan={rescan}
        disabled={start.isPending || remove.isPending}
        hint
        variant="default"
      />
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
          variant="outline"
          size="sm"
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
