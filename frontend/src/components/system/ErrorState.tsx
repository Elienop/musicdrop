// frontend/src/components/system/ErrorState.tsx
import { Error as ErrorIcon } from "@/components/icons";
import { Button } from "@/components/ui/button";

/**
 * The one error recipe (spec §4 — replaces 5 dialects, 3 of them verbatim
 * copies). Retry is ALWAYS rendered: an error surface without a recovery
 * path is a dead end, so the API simply has no retry-less form.
 *
 * - "hero" (default): centered column for a failed page/section body
 *   (the album-grid idiom).
 * - "inline": a single row for a failed widget inside an otherwise-working
 *   page (the duplicates idiom).
 *
 * Both variants are role="alert" — the hero copies forgot it.
 */
export function ErrorState({
  message,
  onRetry,
  variant = "hero",
}: Readonly<{
  message: string;
  onRetry: () => void;
  variant?: "hero" | "inline";
}> ) {
  if (variant === "inline") {
    return (
      <div
        data-slot="error-state"
        role="alert"
        className="border-destructive/40 bg-destructive/5 flex items-center justify-between gap-3 rounded-xl border p-4"
      >
        <p className="text-sm">{message}</p>
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      </div>
    );
  }

  return (
    <div
      data-slot="error-state"
      role="alert"
      className="border-destructive/40 bg-destructive/5 flex flex-col items-center gap-3 rounded-xl border px-4 py-16 text-center"
    >
      <ErrorIcon className="text-destructive size-10" aria-hidden="true" />
      <p className="font-medium">{message}</p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
