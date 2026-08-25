import { useId, useState } from "react";

import type { ImportSearch } from "@/api/useImport";
import { Expand, Refresh, Spinner, Success } from "@/components/icons";
import { ReleaseSearchRow } from "@/components/import/ReleaseSearchRow";
import { Button } from "@/components/ui/button";

export type BarDecision = {
  key: string;
  label: string;
  variant: "ghost" | "secondary";
  onClick: () => void;
  disabled: boolean;
  hinted?: boolean;
};

export type BarPrimary = {
  label: string;
  pendingLabel: string;
  pending: boolean;
  onClick: () => void;
  disabled: boolean;
  icon?: boolean;
};

/** The review screens' sticky command center: decision buttons on the left,
 * the match tools (Different release toggle + Rescan) and the primary action
 * on the right, with the compact search row folded inside the bar. Purely
 * presentational — screens own mutations, disabled unions, and error nodes
 * (passed via `messages`). */
export function ReviewControlBar({
  decisions,
  primary,
  rescan,
  search,
  cluster,
  checking = false,
  hint,
  messages,
}: Readonly<{
  decisions: BarDecision[];
  primary: BarPrimary | null;
  rescan: { onClick: () => void; pending: boolean; disabled: boolean };
  search?: {
    onSearch: (s: ImportSearch) => void;
    busy: boolean;
    feedback: string | null;
    error: boolean;
    defaultOpen?: boolean;
  };
  cluster?: (hintId: string | undefined) => React.ReactNode;
  checking?: boolean;
  hint?: string;
  messages?: React.ReactNode;
}> ) {
  const hintId = useId();
  const formId = useId();
  const [searchOpen, setSearchOpen] = useState(search?.defaultOpen ?? false);

  return (
    <div className="bg-background/80 sticky bottom-0 z-10 -mx-6 -mb-6 flex flex-col gap-1.5 border-t px-6 py-3 shadow-[0_-8px_24px_-16px_rgb(0_0_0/0.35)] backdrop-blur">
      {search && searchOpen && (
        <div className="border-border border-b pb-3">
          <ReleaseSearchRow
            formId={formId}
            onSearch={search.onSearch}
            busy={search.busy}
            feedback={search.feedback}
            error={search.error}
          />
        </div>
      )}
      {checking && (
        <p className="text-muted-foreground text-sm" role="status">
          Checking your library…
        </p>
      )}
      {messages}
      <div className="flex flex-wrap items-center gap-2">
        {decisions.map((d) => (
          <Button
            key={d.key}
            variant={d.variant}
            size="sm"
            disabled={d.disabled}
            aria-describedby={d.hinted && hint ? hintId : undefined}
            onClick={d.onClick}
          >
            {d.label}
          </Button>
        ))}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {search && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-expanded={searchOpen}
              aria-controls={formId}
              onClick={() => setSearchOpen((o) => !o)}
            >
              Different release{" "}
              <Expand
                aria-hidden="true"
                className={searchOpen ? "rotate-180 transition-transform" : "transition-transform"}
              />
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            disabled={rescan.disabled || rescan.pending}
            title="Re-reads the folder from disk and matches it again."
            onClick={rescan.onClick}
          >
            {rescan.pending ? (
              <>
                <Spinner className="animate-spin" aria-hidden="true" /> Rescanning…
              </>
            ) : (
              <>
                <Refresh aria-hidden="true" /> Rescan folder
              </>
            )}
          </Button>
          {cluster?.(hint ? hintId : undefined)}
          {primary && (
            <Button disabled={primary.disabled} onClick={primary.onClick}>
              {primary.pending ? (
                <>
                  <Spinner className="animate-spin" aria-hidden="true" />{" "}
                  {primary.pendingLabel}
                </>
              ) : (
                <>
                  {primary.icon && <Success aria-hidden="true" />}
                  {primary.label}
                </>
              )}
            </Button>
          )}
        </div>
      </div>
      {hint && (
        <p id={hintId} className="text-muted-foreground text-xs">
          {hint}
        </p>
      )}
    </div>
  );
}
