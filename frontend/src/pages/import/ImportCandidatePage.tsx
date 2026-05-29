import {
  AlertCircle,
  Check,
  ChevronDown,
  ExternalLink,
  Loader2,
  Minus,
  Music,
  Pencil,
  Plus,
} from "lucide-react";
import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";

import type { Candidate } from "@/api/useImport";
import {
  importCoverUrl,
  RECOMMENDATION_LABEL,
  useImportCandidate,
  useSubmitChoice,
} from "@/api/useImport";
import { BackLink } from "@/components/albums/album-grid";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

// As-tracks (singleton import) is a silent no-op until Slice B builds real
// per-track import, so hide it rather than offer a button that does nothing.
const AS_TRACKS_ENABLED = false;

export function ImportCandidatePage() {
  const { index: indexParam } = useParams<{ index: string }>();
  const [searchParams] = useSearchParams();
  const jobId = searchParams.get("job") ?? undefined;
  const index = Number(indexParam);
  const validIndex = Number.isInteger(index) && index >= 0;

  // Without a job id (deep link lost the query) or a bad index, there's nothing
  // to fetch — send the user back to the import feed.
  const enabled = Boolean(jobId) && validIndex;
  const { data, isPending, isError, refetch } = useImportCandidate(
    jobId ?? "",
    // `validIndex ? index : 0` keeps the (disabled) query key out of NaN when the
    // index is invalid; the query never fires anyway since `enabled` is false.
    validIndex ? index : 0,
    enabled,
  );

  const backTo = jobId ? `/import?job=${jobId}` : "/import";

  if (!enabled) {
    return (
      <Shell backTo={backTo}>
        <Notice
          title="Nothing to review"
          body="This review link is missing its import job. Go back to the import."
        />
      </Shell>
    );
  }
  if (isPending) {
    return (
      <Shell backTo={backTo}>
        <CandidateSkeleton />
      </Shell>
    );
  }
  if (isError) {
    // A 404 here means the album is no longer parked (already decided / the
    // worker advanced). Treat it as "return to the feed", not a hard error.
    return (
      <Shell backTo={backTo}>
        <Notice
          title="This album isn’t waiting for review"
          body="It may already be decided. Head back to the import to see the feed."
          onRetry={() => void refetch()}
        />
      </Shell>
    );
  }

  return (
    <Shell backTo={backTo}>
      <ReviewScreen
        candidate={data}
        jobId={jobId as string}
        index={index}
        backTo={backTo}
      />
    </Shell>
  );
}

/** Page chrome: the up-link to the feed. */
function Shell({ backTo, children }: { backTo: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-6" aria-label="Review album">
      <BackLink to={backTo} label="Import" />
      {children}
    </section>
  );
}

function ReviewScreen({
  candidate,
  jobId,
  index,
  backTo,
}: {
  candidate: Candidate;
  jobId: string;
  index: number;
  backTo: string;
}) {
  // The candidate index the user will Apply — defaults to the top match (0).
  const [selected, setSelected] = useState(0);

  return (
    <div className="flex flex-col gap-6">
      <MatchHeader candidate={candidate} />
      {candidate.options.length > 1 && (
        <CandidateSwitcher
          options={candidate.options}
          selected={selected}
          onSelect={setSelected}
        />
      )}
      <BeforeAfter candidate={candidate} jobId={jobId} index={index} />
      <WhatChanges candidate={candidate} />
      <Separator />
      <TrackDiff candidate={candidate} />
      <ReviewActions
        jobId={jobId}
        index={index}
        selected={selected}
        backTo={backTo}
      />
    </div>
  );
}

/** `<confidence>% · <recommendation>` + Artist — Album + the source line. */
function MatchHeader({ candidate }: { candidate: Candidate }) {
  const after = candidate.album_after;
  const sourceBits = [
    candidate.data_source,
    after.year?.toString() ?? null,
    after.media,
    after.country,
    after.label,
  ].filter((b): b is string => Boolean(b));
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h2 className="text-2xl font-semibold tracking-tight">
          {after.artist ?? "Unknown artist"} — {after.album ?? "Unknown album"}
        </h2>
      </div>
      <p className="text-muted-foreground flex items-center gap-2 text-sm">
        <span className="text-foreground font-medium">
          {Math.round(candidate.confidence)}%
        </span>
        · {RECOMMENDATION_LABEL[candidate.recommendation]}
        {sourceBits.length > 0 && <span aria-hidden="true">·</span>}
        <span className="truncate">{sourceBits.join(" · ")}</span>
        {candidate.data_url && (
          <a
            href={candidate.data_url}
            target="_blank"
            rel="noreferrer"
            className="text-foreground inline-flex items-center gap-1 underline underline-offset-4"
          >
            view <span className="sr-only">(opens MusicBrainz in a new tab)</span>
            <ExternalLink className="size-3" aria-hidden="true" />
          </a>
        )}
      </p>
    </div>
  );
}

/** Pick a different ranked release to Apply. A native <select> styled as a
 * control — the option text carries % + source + disambiguation. */
function CandidateSwitcher({
  options,
  selected,
  onSelect,
}: {
  options: Candidate["options"];
  selected: number;
  onSelect: (index: number) => void;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-sm font-medium">Other candidates ({options.length})</span>
      <div className="relative w-full max-w-md">
        <select
          value={selected}
          onChange={(e) => onSelect(Number(e.target.value))}
          aria-label="Candidate release"
          className="border-input bg-background focus-visible:border-ring focus-visible:ring-ring/50 h-9 w-full appearance-none rounded-md border px-3 py-2 pr-9 text-sm shadow-xs focus-visible:ring-[3px] focus-visible:outline-none"
        >
          {options.map((opt) => (
            <option key={opt.index} value={opt.index}>
              {Math.round(opt.confidence)}% · {opt.data_source ?? "?"}
              {opt.disambiguation ? ` · ${opt.disambiguation}` : ""}
            </option>
          ))}
        </select>
        <ChevronDown
          className="text-muted-foreground pointer-events-none absolute top-1/2 right-3 size-4 -translate-y-1/2"
          aria-hidden="true"
        />
      </div>
    </label>
  );
}

/** Two calm panels: NOW (your files) vs AFTER import, art-forward. */
function BeforeAfter({
  candidate,
  jobId,
  index,
}: {
  candidate: Candidate;
  jobId: string;
  index: number;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      <AlbumPanel
        heading="Now (your files)"
        change={candidate.album_before}
        coverUrl={candidate.has_current_art ? importCoverUrl(jobId, index) : null}
        coverCaption={candidate.has_current_art ? "Kept on import" : null}
        changedFields={[]}
      />
      <AlbumPanel
        heading="After import"
        change={candidate.album_after}
        coverUrl={candidate.cover_after_url}
        // The matched release's Cover Art Archive image is shown for reference
        // only: with the default config (no fetchart/embedart) the import does
        // not fetch or change cover art, so the existing cover is kept.
        coverCaption={candidate.cover_after_url ? "Release art · not applied" : null}
        changedFields={candidate.changed_fields}
      />
    </div>
  );
}

function AlbumPanel({
  heading,
  change,
  coverUrl,
  coverCaption,
  changedFields,
}: {
  heading: string;
  change: Candidate["album_after"];
  coverUrl: string | null;
  coverCaption: string | null;
  changedFields: string[];
}) {
  const changed = new Set(changedFields);
  return (
    <div className="border-border flex flex-col gap-3 rounded-xl border p-4">
      <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
        {heading}
      </p>
      <div className="flex flex-col gap-1.5">
        <Cover url={coverUrl} />
        {coverCaption && (
          <p className="text-muted-foreground text-xs">{coverCaption}</p>
        )}
      </div>
      <div className="flex flex-col gap-0.5">
        <Field label="Album" value={change.album} changed={changed.has("album")} />
        <Field label="Artist" value={change.artist} changed={changed.has("artist")} />
        <Field
          label="Year"
          value={change.year?.toString() ?? null}
          changed={changed.has("year")}
        />
        <Field label="Label" value={change.label} changed={changed.has("label")} />
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  changed,
}: {
  label: string;
  value: string | null;
  changed: boolean;
}) {
  return (
    <div className="flex items-baseline gap-2 text-sm">
      <span className="text-muted-foreground w-12 shrink-0">{label}</span>
      <span className={cn("truncate", changed && "text-foreground font-medium")}>
        {value ?? "—"}
      </span>
      {changed && (
        <Badge variant="secondary" className="shrink-0">
          changed
        </Badge>
      )}
    </div>
  );
}

/** Serve-or-degrade cover (mirrors AlbumDetailPage.CoverImage). */
function Cover({ url }: { url: string | null }) {
  const [failed, setFailed] = useState(false);
  if (url === null || failed) {
    return (
      <div
        className="bg-muted flex aspect-square w-full items-center justify-center rounded-lg"
        aria-hidden="true"
      >
        <Music className="text-muted-foreground size-10" />
      </div>
    );
  }
  return (
    <img
      src={url}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
      className="bg-muted aspect-square w-full rounded-lg object-cover"
    />
  );
}

/** One-line chip set of what import will change. */
function WhatChanges({ candidate }: { candidate: Candidate }) {
  // Edits import will make (outline chips). Cover art is intentionally NOT
  // listed: with the default config (no fetchart/embedart) the import never
  // fetches or changes art — the after-panel shows the release's art for
  // reference only. (Revisit when the config/art slice can enable fetchart.)
  const changes: string[] = [];
  // `changed_fields` already encodes beets' track-count penalties, so the
  // missing/unmatched counts below are surfaced as their own caveat chips
  // rather than folded into this field list.
  for (const f of candidate.changed_fields) {
    changes.push(f);
  }
  const changedTracks = candidate.tracks.filter((t) => t.status === "changed").length;
  if (changedTracks > 0) {
    changes.push(`${changedTracks} of ${candidate.tracks.length} titles`);
  }

  // Caveats — files that won't line up cleanly with the release (secondary
  // chips, so they read as warnings rather than features).
  const caveats: string[] = [];
  if (candidate.missing.length > 0) {
    caveats.push(`${candidate.missing.length} missing`);
  }
  if (candidate.unmatched.length > 0) {
    caveats.push(`${candidate.unmatched.length} not on release`);
  }

  if (changes.length === 0 && caveats.length === 0) {
    return null;
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-muted-foreground text-sm">Changes</span>
      {changes.map((c) => (
        <Badge key={c} variant="outline">
          {c}
        </Badge>
      ))}
      {caveats.map((c) => (
        <Badge key={c} variant="secondary">
          {c}
        </Badge>
      ))}
    </div>
  );
}

/** Every track current→proposed; changed rows tagged, missing/unmatched flagged. */
function TrackDiff({ candidate }: { candidate: Candidate }) {
  return (
    <section aria-label="Track changes" className="flex flex-col gap-3">
      <h3 className="text-sm font-medium">Tracklist · {candidate.tracks.length}</h3>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-12 pr-4 text-right">#</TableHead>
            <TableHead>Now</TableHead>
            <TableHead>After import</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {candidate.tracks.map((t, i) => {
            const changed = t.status === "changed";
            return (
              <TableRow
                key={t.index ?? `row-${i}`}
                className={cn("hover:bg-transparent", changed && "bg-primary/5")}
              >
                <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
                  {t.track_after ?? t.track_before ?? "–"}
                </TableCell>
                <TableCell className="text-muted-foreground">
                  <span className="truncate">{t.title_before ?? "—"}</span>
                </TableCell>
                <TableCell>
                  <span className="flex min-w-0 items-center gap-2">
                    <span className={cn("truncate", changed && "font-medium")}>
                      {t.title_after ?? "—"}
                    </span>
                    {changed && (
                      <Pencil className="text-muted-foreground size-3 shrink-0" aria-label="changed" />
                    )}
                  </span>
                </TableCell>
              </TableRow>
            );
          })}
          {candidate.missing.map((m, i) => (
            <TableRow key={`missing-${m.index ?? i}`} className="hover:bg-transparent">
              <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
                {m.index ?? "–"}
              </TableCell>
              <TableCell className="text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <Minus className="size-3" aria-hidden="true" /> missing
                </span>
              </TableCell>
              <TableCell className="text-muted-foreground">{m.title ?? "—"}</TableCell>
            </TableRow>
          ))}
          {candidate.unmatched.map((u, i) => (
            <TableRow key={`unmatched-${i}`} className="hover:bg-transparent">
              <TableCell className="text-muted-foreground pr-4 text-right tabular-nums">
                –
              </TableCell>
              <TableCell>
                <span className="inline-flex items-center gap-1">
                  <Plus className="size-3" aria-hidden="true" /> {u.title ?? "—"}
                </span>
              </TableCell>
              <TableCell className="text-muted-foreground">not on release</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}

/** The beets choose_match actions, in a sticky bottom bar. Apply uses the
 * selected candidate index; on success we return to the feed (the worker
 * advances to the next album). */
function ReviewActions({
  jobId,
  index,
  selected,
  backTo,
}: {
  jobId: string;
  index: number;
  selected: number;
  backTo: string;
}) {
  const navigate = useNavigate();
  const submit = useSubmitChoice(jobId);

  function decide(action: "apply" | "skip" | "asis" | "astracks") {
    submit.mutate(
      {
        index,
        choice: {
          action,
          candidate_index: action === "apply" ? selected : null,
        },
      },
      { onSuccess: () => navigate(backTo) },
    );
  }

  return (
    <div className="bg-background/80 sticky bottom-0 z-10 -mx-2 flex flex-col gap-1.5 border-t px-2 py-3 backdrop-blur">
      {/* Picking an alternate candidate changes what Apply submits, but the diff
          above always reflects the top match — say so (there's no undo). */}
      {selected !== 0 && (
        <p className="text-muted-foreground text-sm" role="status">
          Showing the top match — Apply will import the selected release.
        </p>
      )}
      {/* useSubmitChoice swallows 404/409 (already-advanced, navigates anyway);
          a genuine transport error surfaces here instead of silently re-enabling
          the button. */}
      {submit.isError && (
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t submit that choice — try again.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          disabled={submit.isPending}
          onClick={() => decide("skip")}
        >
          Skip
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={submit.isPending}
          title="Import with the current tags, without a MusicBrainz match"
          onClick={() => decide("asis")}
        >
          Use as-is
        </Button>
        {AS_TRACKS_ENABLED && (
          <Button
            variant="outline"
            size="sm"
            disabled={submit.isPending}
            title="Import each file as a standalone track, not grouped as an album"
            onClick={() => decide("astracks")}
          >
            As tracks
          </Button>
        )}
        <Button
          className="ml-auto"
          disabled={submit.isPending}
          onClick={() => decide("apply")}
        >
          {submit.isPending ? (
            <>
              <Loader2 className="animate-spin" aria-hidden="true" /> Applying…
            </>
          ) : (
            <>
              <Check aria-hidden="true" /> Apply
            </>
          )}
        </Button>
      </div>
      <p className="text-muted-foreground text-xs">
        Use as-is keeps your current tags.
      </p>
    </div>
  );
}

function Notice({
  title,
  body,
  onRetry,
}: {
  title: string;
  body: string;
  onRetry?: () => void;
}) {
  return (
    <div className="border-border flex flex-col items-center gap-3 rounded-xl border border-dashed py-16 text-center">
      <AlertCircle className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">{title}</p>
        <p className="text-muted-foreground text-sm">{body}</p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

function CandidateSkeleton() {
  return (
    <>
      {/* role="status" must sit OUTSIDE the aria-hidden skeleton, or screen
          readers never hear the loading announcement (mirrors ImportPage). */}
      <p className="sr-only" role="status">
        Loading the proposed match…
      </p>
      <div className="flex flex-col gap-6" aria-hidden="true">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-7 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Skeleton className="h-64 rounded-xl" />
          <Skeleton className="h-64 rounded-xl" />
        </div>
      </div>
    </>
  );
}
