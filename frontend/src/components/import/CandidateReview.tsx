import type { Candidate } from "@/api/useImport";
import { RECOMMENDATION_LABEL } from "@/api/useImport";
import {
  Add,
  Edit as EditIcon,
  Expand,
  External,
  Missing,
} from "@/components/icons";
import { CoverArt } from "@/components/system/CoverArt";
import { SectionLabel } from "@/components/system/SectionLabel";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

/**
 * The presentational candidate-review body — everything between the back
 * link and the actions bar: match header (owns the page h1), candidate
 * switcher, before/after panels, change chips, track diff. Pure render over
 * a `Candidate`: the LIVE page feeds it from the job endpoints and a
 * job-scoped current-art URL; the BANK page feeds it from a banked
 * `ParkedAlbum.candidate` with `nowCoverUrl={null}` (the live art endpoint
 * died with the sweep job; banked rows keep only the metadata payload —
 * `cover_after_url` is an absolute Cover Art Archive URL and still renders).
 * Actions stay caller-owned: the two pages submit to different APIs.
 */
export function CandidateReview({
  candidate,
  nowCoverUrl,
  selected,
  onSelect,
}: {
  candidate: Candidate;
  nowCoverUrl: string | null;
  selected: number;
  onSelect: (index: number) => void;
}) {
  return (
    <div className="flex flex-col gap-6">
      <MatchHeader candidate={candidate} />
      {candidate.options.length > 1 && (
        <CandidateSwitcher
          options={candidate.options}
          selected={selected}
          onSelect={onSelect}
        />
      )}
      <BeforeAfter candidate={candidate} nowCoverUrl={nowCoverUrl} />
      <WhatChanges candidate={candidate} />
      <Separator />
      <TrackDiff candidate={candidate} />
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
        <h1 tabIndex={-1} className="font-display text-display font-semibold tracking-tight">
          {after.artist ?? "Unknown artist"} — {after.album ?? "Unknown album"}
        </h1>
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
            <External className="size-3" aria-hidden="true" />
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
        <Expand
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
  nowCoverUrl,
}: {
  candidate: Candidate;
  nowCoverUrl: string | null;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      <AlbumPanel
        heading="Now (your files)"
        change={candidate.album_before}
        coverUrl={nowCoverUrl}
        coverCaption={nowCoverUrl !== null ? "Kept on import" : null}
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
        <CoverArt src={coverUrl} className="w-full rounded-lg" />
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
      <SectionLabel>Tracklist · {candidate.tracks.length}</SectionLabel>
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
                      <>
                        <EditIcon
                          className="text-muted-foreground size-3 shrink-0"
                          aria-hidden="true"
                        />
                        <span className="sr-only">changed</span>
                      </>
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
                  <Missing className="size-3" aria-hidden="true" /> missing
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
                  <Add className="size-3" aria-hidden="true" /> {u.title ?? "—"}
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
