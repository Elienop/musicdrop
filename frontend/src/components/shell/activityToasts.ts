// frontend/src/components/shell/activityToasts.ts
//
// Cross-page job-outcome toasts (spec §2). ONE effect, called exactly once —
// in App, as useActivityToasts(useActivity().rows) — that diffs each row's
// state against the previous render via a ref. Only transitions this hook
// has itself OBSERVED fire: running→done → toast.success, running→failed →
// toast.error. The first render after a mount/reload only baselines (rows
// may already be terminal — failed rows persist in the popover — and
// re-announcing them would be a toast storm). Rows that vanish without a
// terminal state (the import probe has no phase) stay silent.
import { useEffect, useRef } from "react";
import { toast } from "sonner";

import type { ActivityRow } from "@/api/useActivity";

export function useActivityToasts(rows: ActivityRow[]): void {
  const previous = useRef<Map<string, ActivityRow["state"]> | null>(null);

  useEffect(() => {
    const next = new Map(rows.map((row) => [row.id, row.state] as const));
    const before = previous.current;
    previous.current = next;
    if (before === null) {
      return; // First render: baseline silently — never a reload toast storm.
    }
    for (const row of rows) {
      if (before.get(row.id) !== "running") {
        continue; // Unobserved or already-terminal last render → no toast.
      }
      if (row.state === "done") {
        toast.success(
          row.countsText !== undefined
            ? `${row.label} — ${row.countsText}`
            : `${row.label} finished`,
        );
      } else if (row.state === "failed") {
        toast.error(
          row.countsText !== undefined
            ? `${row.label} failed — ${row.countsText}`
            : `${row.label} failed`,
        );
      }
    }
  }, [rows]);
}
