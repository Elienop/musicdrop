import type { ImportJobState } from "@/api/useImport";

/** The single spoken status for the whole run. Verb-first and intentionally
 * worded differently from the visible cue/panels so it never substring-collides
 * with them in the DOM — it is the one `aria-live` source. */
export function announceMessage(args: {
  isPending: boolean;
  isError: boolean;
  notFound: boolean;
  data: ImportJobState | undefined;
}): string {
  const { isPending, isError, notFound, data } = args;
  if (notFound) return "This import is no longer available.";
  if (isError) return "Couldn't load the import.";
  if (isPending || !data) return "Loading the import.";
  if (data.phase === "failed") return "The import failed.";
  if (data.phase === "done") {
    const { applied, skipped } = data.progress;
    return `Import complete. Imported ${applied}, skipped ${skipped}.`;
  }
  if (data.phase === "scanning" && data.albums.length === 0) {
    return "Scanning the folder for albums.";
  }
  const { applied, skipped, needs_review } = data.progress;
  let m = `Imported ${applied}.`;
  if (skipped > 0) m += ` Skipped ${skipped}.`;
  if (needs_review > 0) m += " One album awaiting review.";
  return m;
}
