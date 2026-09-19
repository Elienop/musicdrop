import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import type { NavigateFunction } from "react-router";

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
import { startErrorSentence, useStartImport } from "@/api/useImport";
import { BackLink } from "@/components/albums/album-grid";
import { AlreadyInLibrary } from "@/components/import/AlreadyInLibrary";
import { CandidateReview } from "@/components/import/CandidateReview";
import {
  DUPLICATE_FOOTNOTE_BANK,
  DuplicateActionRow,
  DuplicateActions,
  DuplicateComparison,
} from "@/components/import/DuplicateReview";
import {
  ReviewControlBar,
  type BarDecision,
} from "@/components/import/ReviewControlBar";
import { Info, Refresh, Remove, Spinner, Success, Warning } from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
import { ErrorState } from "@/components/system/ErrorState";
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
  const { data, isPending, isError, error, refetch } = useBankItem(itemId);
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
  if (error instanceof BankConflictError) {
    // 404 = removed, applied away, or a dead deep link — return to the list.
    return (
      <Shell>
        <EmptyState
          bordered
          icon={Info}
          title="This row is no longer in the bank"
          body="It may have been applied, ignored, or removed. Head back to see what’s waiting."
          action={
            <Button variant="outline" size="sm" asChild>
              <Link to="/review">Back to Review</Link>
            </Button>
          }
        />
      </Shell>
    );
  }
  if (isError || !data) {
    // A transient failure (5xx / network blip, e.g. a backend restart while the
    // apply runner hammers the disk) — offer a retry rather than falsely
    // declaring the row applied/removed.
    return (
      <Shell>
        <ErrorState
          message="Couldn’t load this banked row."
          onRetry={() => void refetch()}
        />
      </Shell>
    );
  }
  return <BankScreen item={data} />;
}

/** Page chrome: bank rows are always entered from Review — fixed up-link.
 * `toolbar` (the screens' Rescan control) rides the back-link row, top-right. */
function Shell({
  toolbar,
  children,
}: Readonly<{
  toolbar?: React.ReactNode;
  children: React.ReactNode;
}> ) {
  return (
    <section className="flex flex-col gap-6" aria-label="Review banked album">
      <div className="flex items-start justify-between gap-4">
        <BackLink to="/review" label="Review" />
        {toolbar}
      </div>
      {children}
    </section>
  );
}

function BankScreen({ item }: Readonly<{ item: BankItem }>) {
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
function DecisionError({ error }: Readonly<{ error: unknown }>) {
  if (!error) return null;
  const message =
    error instanceof BankConflictError
      ? error.message
      : "Couldn’t submit that decision. Try again.";
  return (
    <p className="text-destructive text-sm" role="alert">
      {message}
    </p>
  );
}

/** The search's conflict line — a 409's real detail (stale flip, status race).
 * Non-conflict transport errors render inside the panel instead. */
function SearchConflict({ error }: Readonly<{ error: unknown }>) {
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
 * row's new state. Rendered in the Shell's top-right toolbar; the hook is
 * hoisted to each screen so its pending state can busy-gate the screen's
 * other actions. */
function RescanControl({
  rescan,
  disabled,
  variant = "outline",
}: Readonly<{
  rescan: ReturnType<typeof useBankRescan>;
  disabled: boolean;
  variant?: "outline" | "default";
}> ) {
  return (
    <div className="flex shrink-0 flex-col items-end gap-1.5">
      <Button
        variant={variant}
        size="sm"
        disabled={disabled || rescan.isPending}
        title="Re-reads the folder from disk and matches it again."
        onClick={() => rescan.mutate()}
      >
        {rescan.isPending ? (
          <>
            <Spinner className="animate-spin" aria-hidden="true" /> Rescanning…
          </>
        ) : (
          <>
            <Refresh aria-hidden="true" /> Rescan folder
          </>
        )}
      </Button>
      <SearchConflict error={rescan.error} />
      {rescan.isError && !(rescan.error instanceof BankConflictError) && (
        <p className="text-destructive text-sm" role="alert">
          Couldn’t rescan the folder. Try again.
        </p>
      )}
    </div>
  );
}

const NO_HIT_FEEDBACK = "No release found. Showing your previous matches.";

/** The failed-apply banner. role=alert via StatusBanner's destructive tone.
 *
 * The headline branches on the row's `error_retryable` flag, never on the
 * error TEXT — the same stance the strip below takes, for the same reason.
 * The runner marks a row not-retryable when deciding again would import a
 * THIRD copy, so the emphasized line must not instruct the retry that the
 * muted reason under it forbids; recovery is removing one of the two copies,
 * which the Duplicates link reaches. Absent or true keeps the retry headline —
 * rows banked before the flag existed are retryable. */
function FailedBanner({ item }: Readonly<{ item: BankItem }>) {
  const retryable = item.error_retryable !== false;
  const failure = (item.error ?? "").trim();
  return (
    <StatusBanner
      tone="destructive"
      icon={Warning}
      action={
        retryable ? undefined : (
          <Button variant="outline" size="sm" asChild>
            <Link to="/duplicates">Open Duplicates</Link>
          </Button>
        )
      }
    >
      <p className="font-medium">
        {retryable
          ? "The apply failed. Decide again to retry."
          : "The apply failed."}
      </p>
      {/* This is the diagnosis surface, so the string is NOT clamped — only
          stopped from painting out of its column. `break-words` breaks the
          unbroken paths beets puts in these messages; it lowers no ancestor's
          min-content floor, which is why the `min-w-0 flex-1` column
          StatusBanner puts this in is enough on its own (the row's version
          needs an extra `min-w-0`, because there the text is a flex ITEM).
          Trimmed only for the guard: a whitespace-only `str(exc)` must render
          nothing rather than an empty line under the headline. */}
      {failure !== "" && (
        <p className="text-muted-foreground text-sm break-words">{item.error}</p>
      )}
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
}: Readonly<{
  busy: boolean;
  pending: DuplicateAction | null;
  onDecide: (action: DuplicateAction) => void;
}> ) {
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

/** One shared decision submitter: post the decision, return to Review on
 * success, and clear any pinned duplicate action on error. */
function makeSubmit(
  decide: ReturnType<typeof useBankDecision>,
  navigate: NavigateFunction,
  setPendingDup: React.Dispatch<React.SetStateAction<DuplicateAction | null>>,
) {
  return (decision: BankDecision) =>
    decide.mutate(decision, {
      onSuccess: () => navigate("/review"),
      onError: () => setPendingDup(null),
    });
}

/** The bar's cluster slot, hoisted to module level so no component-shaped
 * closure is defined inside a screen component. ReviewControlBar CALLS
 * `cluster(hintId)` as a function, so this renders exactly where the row
 * belongs; the DOM is identical to the former inline arrows. */
function BankDuplicateCluster({
  hintId,
  pending,
  busy,
  onDecide,
}: Readonly<{
  hintId: string | undefined;
  pending: DuplicateAction | null;
  busy: boolean;
  onDecide: (action: DuplicateAction) => void;
}> ) {
  return (
    <DuplicateActionRow
      pending={pending}
      busy={busy}
      describedBy={hintId}
      onDecide={onDecide}
    />
  );
}

/** Build the bar's cluster render-prop OUTSIDE any component, so screens pass
 * a ready-made function instead of defining one inline (S6478). */
function makeBankDuplicateCluster(
  props: Readonly<{
    pending: DuplicateAction | null;
    busy: boolean;
    onDecide: (action: DuplicateAction) => void;
  }>,
) {
  return (hintId: string | undefined) => (
    <BankDuplicateCluster hintId={hintId} {...props} />
  );
}

function BankCandidateScreen({ item }: Readonly<{ item: BankItem }>) {
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

  const submit = makeSubmit(decide, navigate, setPendingDup);

  const onDecideDup = (action: DuplicateAction) => {
    setPendingDup(action);
    submit({
      action: "duplicate",
      candidate_index: selected,
      duplicate_action: action,
    });
  };

  const busyAll = decide.isPending || search.isPending || rescan.isPending;
  const ignore: BarDecision = {
    key: "ignore",
    label: "Ignore",
    variant: "ghost",
    onClick: () => submit({ action: "ignore" }),
    disabled: busyAll,
  };

  return (
    <Shell>
      <div className="flex flex-col gap-6">
        {item.status === "failed" && <FailedBanner item={item} />}
        <CandidateReview
          candidate={parked.candidate}
          // Banked rows keep metadata only — the live job's current-art
          // endpoint died with the sweep. The placeholder is the honest state;
          // the after-panel's Cover Art Archive URL still renders.
          nowCoverUrl={null}
          selected={selected}
          onSelect={setSelected}
        />
        {showDupActions && (
          <>
            {hasCollision ? (
              <AlreadyInLibrary
                existing={existing}
                blurb={
                  existing.length === 1
                    ? "This album matches one you already have. Choose what to do below; your choice imports the selected release."
                    : `This album matches ${existing.length} you already have. Choose what to do below; your choice imports the selected release.`
                }
              />
            ) : (
              // Error-on-failed fallback: the re-check couldn't run, so we can't
              // list the colliding copies, but the failed-banner already told the
              // user to resolve it as a duplicate — keep that path reachable.
              <section aria-label="Resolve as a duplicate" className="flex flex-col gap-2">
                <SectionLabel>Resolve as a duplicate</SectionLabel>
                <p className="text-muted-foreground text-sm">
                  Couldn’t re-check your library. If the apply failed because
                  this album is already in it, resolve it as a duplicate below.
                </p>
              </section>
            )}
          </>
        )}
        <ReviewControlBar
          decisions={
            showDupActions
              ? [ignore]
              : [
                  ignore,
                  {
                    key: "asis",
                    label: "Use as-is",
                    variant: "secondary",
                    hinted: true,
                    onClick: () => submit({ action: "asis" }),
                    disabled: busyAll || checking,
                  },
                  {
                    key: "astracks",
                    label: "As tracks",
                    variant: "secondary",
                    hinted: true,
                    onClick: () => submit({ action: "astracks" }),
                    disabled: busyAll || checking,
                  },
                ]
          }
          primary={
            showDupActions
              ? null
              : {
                  label: "Apply",
                  pendingLabel: "Queuing…",
                  pending: decide.isPending,
                  icon: true,
                  onClick: () => submit({ action: "apply", candidate_index: selected }),
                  disabled: busyAll || checking,
                }
          }
          rescan={{
            onClick: () => rescan.mutate(),
            pending: rescan.isPending,
            disabled: decide.isPending || search.isPending,
          }}
          search={{
            onSearch: (s) => search.mutate(s),
            busy: busyAll,
            feedback: search.data?.found === false ? NO_HIT_FEEDBACK : null,
            error: search.isError && !(search.error instanceof BankConflictError),
          }}
          cluster={
            showDupActions
              ? makeBankDuplicateCluster({
                  pending: pendingDup,
                  busy: busyAll,
                  onDecide: onDecideDup,
                })
              : undefined
          }
          checking={!showDupActions && checking}
          hint={
            showDupActions
              ? DUPLICATE_FOOTNOTE_BANK
              : "Decisions queue until the import slot is free. Use as-is keeps your tags; As tracks imports files individually."
          }
          messages={
            <>
              {hasCollision && (
                <output className="text-muted-foreground text-sm block">
                  This album is already in your library.
                </output>
              )}
              {/* Same unpinned-apply hazard as the duplicate screen, keyed on
                  the SELECTED option — both submit shapes here carry
                  candidate_index, so this is exactly what the apply pins to. */}
              {parked.candidate.options[selected]?.release_id == null && (
                <output className="text-muted-foreground text-sm block">
                  {UNPINNED_OPTION_NOTE}
                </output>
              )}
              <DecisionError error={decide.error} />
              <SearchConflict error={search.error} />
              <SearchConflict error={rescan.error} />
              {rescan.isError && !(rescan.error instanceof BankConflictError) && (
                <p className="text-destructive text-sm" role="alert">
                  Couldn’t rescan the folder. Try again.
                </p>
              )}
            </>
          }
        />
      </div>
    </Shell>
  );
}

/**
 * The honest note for a duplicate row whose resolution is NOT pinned to the
 * release on screen. Deliberately avoids "Apply" (this screen has no Apply
 * button — the four duplicate actions ARE the submit) and avoids "the release
 * you chose": the banked release was matched by the sweep, not picked by the
 * user, so it is only ever the one SHOWN. Shape-neutral on purpose — "no
 * release id is stored" is true both for a row banked before matches were
 * stored AND for a stored match from an id-less source — and it names the
 * on-screen remedy: Rescan re-banks the row through the pinning path (fresh
 * payload; the collision is re-flagged by the up-front check if still real).
 */
const LEGACY_REMATCH_NOTE =
  "No release id is stored for this row: importing it re-matches the folder online, so the release that lands may differ from the one shown. Rescan the folder to see a fresh match.";

/**
 * The candidate screen's sibling of the note above, keyed on the SELECTED
 * option — the user picks `candidate_index` on that screen, so keying index 0
 * would go silently wrong the moment they switch. "Apply" is accurate there
 * (the button exists). No rescan sentence: the natural remedy on that screen
 * is picking an option that carries a release id, when one exists.
 */
const UNPINNED_OPTION_NOTE =
  "The selected match has no stored release id: Apply re-matches the folder online, so the release that lands may differ from the one shown.";

/**
 * Is this row's resolution unpinned — i.e. will the apply runner re-run the
 * match online instead of importing the release this screen displays?
 *
 * The sweep now banks the candidate payload alongside the duplicate prompt, so
 * a modern row pins to its stored release like every other row. Two shapes
 * cannot pin: no parked payload at all (banked before the fix), and a stored
 * option from an id-less source (`release_id` null — nothing to pin TO, and
 * possible on rows of any age).
 *
 * Keyed on the row's STORED DATA, never on `status`: a failed apply re-enters
 * this screen as `failed` and is still legacy on every retry.
 *
 * Neither a missing payload nor an empty `options` array needs a clause of
 * its own — the optional chain makes both read as `undefined == null` → true.
 */
function isLegacyUnpinned(item: BankItem): boolean {
  return item.parked?.candidate.options[0]?.release_id == null;
}

function BankDuplicateScreen({ item }: Readonly<{ item: BankItem }>) {
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
    <Shell>
      <div className="flex flex-col gap-6">
        {item.status === "failed" && <FailedBanner item={item} />}
        <DuplicateComparison prompt={prompt} incomingCoverUrl={null} />
        <ReviewControlBar
          decisions={[
            {
              key: "ignore",
              label: "Ignore",
              variant: "ghost",
              onClick: () =>
                decide.mutate(
                  { action: "ignore" },
                  { onSuccess: () => navigate("/review") },
                ),
              disabled: decide.isPending || rescan.isPending,
            },
          ]}
          primary={null}
          rescan={{
            onClick: () => rescan.mutate(),
            pending: rescan.isPending,
            disabled: decide.isPending,
          }}
          hint={DUPLICATE_FOOTNOTE_BANK}
          cluster={makeBankDuplicateCluster({
            pending,
            busy: decide.isPending || rescan.isPending,
            onDecide,
          })}
          messages={
            <>
              {/* Sits with the other decision-point messages, directly above
                  the four duplicate actions it qualifies. */}
              {isLegacyUnpinned(item) && (
                <output className="text-muted-foreground text-sm block">
                  {LEGACY_REMATCH_NOTE}
                </output>
              )}
              <DecisionError error={decide.error} />
              <SearchConflict error={rescan.error} />
              {rescan.isError && !(rescan.error instanceof BankConflictError) && (
                <p className="text-destructive text-sm" role="alert">
                  Couldn’t rescan the folder. Try again.
                </p>
              )}
            </>
          }
        />
      </div>
    </Shell>
  );
}

function NoMatchScreen({ item }: Readonly<{ item: BankItem }>) {
  const navigate = useNavigate();
  const decide = useBankDecision(item.id);
  const search = useBankSearch(item.id);
  const rescan = useBankRescan(item.id);
  const [pendingDup, setPendingDup] = useState<DuplicateAction | null>(null);
  const submit = makeSubmit(decide, navigate, setPendingDup);
  const busyAll = decide.isPending || search.isPending || rescan.isPending;

  return (
    <Shell>
      <div className="flex flex-col gap-6">
        {item.status === "failed" && <FailedBanner item={item} />}
        <div className="flex flex-col gap-1">
          <h1 tabIndex={-1} className="font-display text-display font-semibold tracking-tight">
            No match found
          </h1>
          <p className="text-muted-foreground text-sm">
            beets couldn&rsquo;t match this folder against MusicBrainz. Search
            for the right release, import it as-is or as tracks, or ignore it.
          </p>
        </div>
        <p className="text-muted-foreground font-mono text-xs" title={item.folder}>
          {item.folder}
        </p>
        {item.status === "failed" && (
          <FailedDuplicateStrip
            busy={busyAll}
            pending={pendingDup}
            onDecide={(action) => {
              setPendingDup(action);
              submit({ action: "duplicate", duplicate_action: action });
            }}
          />
        )}
        <ReviewControlBar
          decisions={[
            {
              key: "ignore",
              label: "Ignore",
              variant: "ghost",
              onClick: () => submit({ action: "ignore" }),
              disabled: busyAll,
            },
            {
              key: "astracks",
              label: "As tracks",
              variant: "secondary",
              onClick: () => submit({ action: "astracks" }),
              disabled: busyAll,
            },
          ]}
          primary={{
            label: "Use as-is",
            pendingLabel: "Queuing…",
            pending: decide.isPending,
            onClick: () => submit({ action: "asis" }),
            disabled: busyAll,
          }}
          rescan={{
            onClick: () => rescan.mutate(),
            pending: rescan.isPending,
            disabled: decide.isPending || search.isPending,
          }}
          search={{
            onSearch: (s) => search.mutate(s),
            busy: busyAll,
            feedback: search.data?.found === false ? NO_HIT_FEEDBACK : null,
            error: search.isError && !(search.error instanceof BankConflictError),
            defaultOpen: true,
          }}
          messages={
            <>
              <DecisionError error={decide.error} />
              <SearchConflict error={search.error} />
              <SearchConflict error={rescan.error} />
              {rescan.isError && !(rescan.error instanceof BankConflictError) && (
                <p className="text-destructive text-sm" role="alert">
                  Couldn’t rescan the folder. Try again.
                </p>
              )}
            </>
          }
        />
      </div>
    </Shell>
  );
}

/** queued/applying: the apply runner owns the row; the hook polls (2s) so
 * this screen progresses to done/failed live. No actions — deciding 409s and
 * deleting an applying row 409s.
 *
 * The queued body says only that the decision is carried out for you, because
 * the two more specific claims it used to make are both false for a resolution
 * that imports nothing: an enforced skip_new never takes the import slot (so
 * it does not "wait" for it) and moves no files. One sentence that holds for
 * all four duplicate actions beats two that hold for three of them. */
function PendingNotice({ item }: Readonly<{ item: BankItem }>) {
  const applying = item.status === "applying";
  return (
    <Shell>
      <EmptyState
        bordered
        icon={Info}
        title={applying ? "Applying now" : "Queued to apply"}
        body={
          applying
            ? "beets is importing this folder; this page updates when it lands."
            : "This decision will be applied automatically; no further action needed."
        }
      />
    </Shell>
  );
}

function DoneNotice({ item }: Readonly<{ item: BankItem }>) {
  const { title, body } = doneOutcome(item);
  return (
    <Shell>
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
    </Shell>
  );
}

/**
 * Title + body describing what the resolved row actually did, read from the
 * decision AND the outcome. A skip_new duplicate resolution normally KEPT your
 * copy — it never "landed in your library", so it must not claim it did.
 *
 * But the decision alone does not settle what happened. An enforced skip_new
 * resolves without importing only while the stored library copy SURVIVES; if
 * the user deleted or moved it between banking and applying there is nothing
 * left to keep, the import proceeds, and the row lands an album. `album_id` is
 * the only witness of which of the two ran, which is why the skip_new arm
 * branches on it: keyed on the decision alone this notice would print "Nothing
 * new was imported" directly beside DoneNotice's View-album button for the
 * album that just imported.
 */
function doneOutcome(item: BankItem): { title: string; body: string } {
  const label = `${item.artist ?? "Unknown artist"} - ${item.album ?? lastSegment(item.folder)}`;
  const decided = item.decided;
  if (decided?.action === "duplicate") {
    switch (decided.duplicate_action) {
      case "skip_new":
        return item.album_id == null
          ? {
              title: "Kept your existing copy",
              body: "Nothing new was imported; your existing copy is untouched.",
            }
          : {
              // Same noun phrase as the arm above on purpose — the two read as
              // a matched pair, and the difference is the whole message.
              title: "Imported — your existing copy was gone",
              body: `The copy you chose to keep was no longer in your library, so ${label} was imported.`,
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

function IgnoredNotice({ item }: Readonly<{ item: BankItem }>) {
  return (
    <Shell>
      <EmptyState
        bordered
        icon={Info}
        title="Ignored"
        body="This folder was left as-is. Remove the row to clear it from the list; the files are untouched."
        action={<RemoveRowButton itemId={item.id} />}
      />
    </Shell>
  );
}

/** Remove-the-row action shared by the settled notices. */
function RemoveRowButton({ itemId }: Readonly<{ itemId: string }>) {
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
/** The generic half of a failed re-scan start — the only part that is this
 * screen's own. The refusals (409 / 422 / 503) carry the server's own reason
 * through {@link startErrorSentence}: a 409 has three different causes and
 * naming the wrong one sent the user off to wait for an import that was not
 * running. */
const START_FAILED = "Couldn’t start the re-scan. Check the backend, then try again.";

/** The id linking that sentence to the button it belongs to — "Review now"
 * keeps focus through a failed start (it is only `aria-disabled` while
 * pending), so the alert is what a keyboard user hears on coming back to it.
 * One screen, one alert, like the import panel's. */
const STALE_START_ERROR_ID = "stale-start-error";

function StaleScreen({ item }: Readonly<{ item: BankItem }>) {
  const navigate = useNavigate();
  const start = useStartImport();
  const remove = useDeleteBankItem();
  const rescan = useBankRescan(item.id);
  const active = useActiveImport();
  const importActive = active.data?.active ?? false;
  const busy = start.isPending || remove.isPending || rescan.isPending;

  function reviewNow() {
    start.mutate(
      {
        path: item.folder,
        // `incremental: false` is beets' own `-I`. The sweep that banked this
        // folder recorded it in beets' import history, so without the override a
        // run that keeps its files skips every album here — and the row is
        // deleted on success either way, leaving the album in neither the bank
        // nor the library. The other three fields are the manual default, which
        // the generated ImportOptions marks required.
        options: {
          operation: "default",
          unattended: false,
          sweep: false,
          incremental: false,
        },
      },
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

  const startError = startErrorSentence(start.error, start.isError, START_FAILED);

  return (
    <Shell
      toolbar={
        <RescanControl
          rescan={rescan}
          disabled={start.isPending || remove.isPending}
          variant="default"
        />
      }
    >
    <div className="flex flex-col gap-6">
      <StatusBanner tone="warning" icon={Warning}>
        <p className="font-medium">This folder changed after it was banked.</p>
        <p className="text-muted-foreground text-sm">
          {item.error ?? "The banked candidates no longer match the files."} Rescan
          the folder to review fresh matches; the banked ones are out of date.
        </p>
      </StatusBanner>
      <p className="text-muted-foreground font-mono text-xs" title={item.folder}>
        {item.folder}
      </p>
      {/* Not while an import runs: the hint under the buttons is the fuller
          sentence (it names the slot AND what still works), and the server's
          409 opens on the same clause — the two stacked in one column repeated
          "an import is already running" twice, the alert first and shorter. */}
      {startError && !importActive && (
        // `break-words`: a 503 here carries repr'd paths, which Chromium will
        // not break at `/` (the Trash page's measured family).
        <p
          id={STALE_START_ERROR_ID}
          className="text-destructive text-sm break-words"
          role="alert"
        >
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
          variant="outline"
          size="sm"
          className="ml-auto aria-disabled:opacity-50"
          // The pending half is `aria-disabled`, not `disabled`: this button
          // holds focus when it is clicked, and disabling it on that commit
          // strands keyboard focus on <body> (the Pagination rule — the import
          // page's Pause button carries the measurement). The other two
          // mutations and a running import belong to controls elsewhere, so they
          // stay `disabled`. The click is swallowed below.
          disabled={remove.isPending || rescan.isPending || importActive}
          aria-disabled={start.isPending}
          // Both descriptions, joined — the import-slot hint and the failure
          // sentence. They are mutually exclusive today (the alert is gated on
          // `!importActive` above), so this is the shape rather than a second
          // branch to keep in step with that gate.
          aria-describedby={
            [
              importActive ? "stale-rescan-hint" : null,
              startError && !importActive ? STALE_START_ERROR_ID : null,
            ]
              .filter((id) => id !== null)
              .join(" ") || undefined
          }
          onClick={() => {
            if (busy) return;
            reviewNow();
          }}
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
          An import is already running; the re-scan needs the import slot.
          Decisions on other banked rows still work meanwhile.
        </p>
      )}
    </div>
    </Shell>
  );
}

/** Last path segment, for rows without a parsed album title. */
function lastSegment(folder: string): string {
  const parts = folder.split("/").filter(Boolean);
  return parts.at(-1) ?? folder;
}
