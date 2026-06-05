// frontend/src/components/ReorganizeBanner.tsx
import { AlertCircle, Loader2 } from "lucide-react";

import { useReorganizeStatus } from "@/api/useReorganize";

export function ReorganizeBanner() {
  const { data } = useReorganizeStatus();

  if (data?.phase === "running") {
    return (
      <div
        className="border-border bg-muted/50 mb-6 flex items-center gap-3 rounded-xl border p-3 text-sm"
        role="status"
      >
        <Loader2 className="text-muted-foreground size-5 shrink-0 animate-spin" aria-hidden="true" />
        <span className="flex-1">
          {`Reorganizing — ${data.scope_label}… ${data.processed} / ${data.total}`}
        </span>
      </div>
    );
  }

  if (data?.phase === "failed") {
    return (
      <div
        className="border-destructive/40 bg-destructive/5 mb-6 flex items-center gap-3 rounded-xl border p-3 text-sm"
        role="alert"
      >
        <AlertCircle className="text-destructive size-5 shrink-0" aria-hidden="true" />
        <span className="flex-1">Reorganize failed{data.error ? `: ${data.error}` : "."}</span>
      </div>
    );
  }

  return null;
}
