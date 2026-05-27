import { AlertCircle, ChevronDown, ExternalLink, Music } from "lucide-react";
import { useState } from "react";
import { useParams, useSearchParams } from "react-router";

import type { Candidate } from "@/api/useImport";
import {
  importCoverUrl,
  RECOMMENDATION_LABEL,
  useImportCandidate,
} from "@/api/useImport";
import { BackLink } from "@/components/albums/album-grid";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

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
      <ReviewScreen candidate={data} jobId={jobId as string} index={index} />
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
}: {
  candidate: Candidate;
  jobId: string;
  index: number;
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
      {/* Task 6 inserts <TrackDiff/> and the sticky <ReviewActions/> here,
          using `selected`, `jobId`, `index`, and `backTo`. */}
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
            view <ExternalLink className="size-3" aria-hidden="true" />
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
          className="border-input bg-background w-full appearance-none rounded-md border px-3 py-2 pr-9 text-sm"
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
        changedFields={[]}
      />
      <AlbumPanel
        heading="After import"
        change={candidate.album_after}
        coverUrl={candidate.cover_after_url}
        changedFields={candidate.changed_fields}
      />
    </div>
  );
}

function AlbumPanel({
  heading,
  change,
  coverUrl,
  changedFields,
}: {
  heading: string;
  change: Candidate["album_after"];
  coverUrl: string | null;
  changedFields: string[];
}) {
  const changed = new Set(changedFields);
  return (
    <div className="border-border flex flex-col gap-3 rounded-xl border p-4">
      <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
        {heading}
      </p>
      <Cover url={coverUrl} />
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
  const chips: string[] = [];
  if (candidate.cover_after_url && !candidate.has_current_art) {
    chips.push("+ cover art");
  }
  for (const f of candidate.changed_fields) {
    chips.push(f);
  }
  const changedTracks = candidate.tracks.filter((t) => t.status === "changed").length;
  if (changedTracks > 0) {
    chips.push(`${changedTracks} of ${candidate.tracks.length} titles`);
  }
  if (chips.length === 0) {
    return null;
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-muted-foreground text-sm">Changes</span>
      {chips.map((c) => (
        <Badge key={c} variant="outline">
          {c}
        </Badge>
      ))}
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
    <div className="flex flex-col gap-6" aria-hidden="true">
      <p className="sr-only" role="status">
        Loading the proposed match…
      </p>
      <div className="flex flex-col gap-2">
        <Skeleton className="h-7 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Skeleton className="h-64 rounded-xl" />
        <Skeleton className="h-64 rounded-xl" />
      </div>
    </div>
  );
}
