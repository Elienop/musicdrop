import { Wrench } from "lucide-react";
import { useParams } from "react-router";

import { BackLink } from "@/components/albums/album-grid";

/**
 * Seam destination for `/import/albums/:index` (chunk-3 ↔ chunk-4 boundary).
 *
 * Chunk 3 ships this STUB so the Review affordance in the live feed has a real,
 * navigable target — keeping the feed page testable and screenshot-able. Chunk
 * 4 replaces the body with the faithful candidate-review screen (before/after +
 * full tracklist + the choice actions), driven by `useImportCandidate` +
 * `useSubmitChoice` (both already built in chunk 3).
 */
export function ImportCandidatePage() {
  const { index } = useParams<{ index: string }>();

  return (
    <section className="flex flex-col gap-6" aria-label="Review album">
      <BackLink to="/import" label="Import" />
      <div className="border-border flex flex-col items-center gap-3 rounded-xl border border-dashed py-16 text-center">
        <Wrench className="text-muted-foreground size-10" aria-hidden="true" />
        <div className="flex flex-col gap-1">
          <p className="font-medium">Review screen coming soon</p>
          <p className="text-muted-foreground text-sm">
            The candidate review for album {index} arrives in the next slice.
          </p>
        </div>
      </div>
    </section>
  );
}
