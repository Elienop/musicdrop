import { Button } from "@/components/ui/button";

/**
 * STUB — Task 12 replaces this with a `@codemirror/merge` MergeView.
 *
 * For the T11 page integration we just need a typecheck-clean component with
 * the right prop surface and the two terminal actions ("Reload" drops local
 * edits, "Overwrite" re-Saves with the server's fresh CAS tokens). The page's
 * conflict-handling wiring is the part T11 is actually validating.
 */
export interface SettingsConflictProps {
  local: string;
  server: string;
  onReload: () => void;
  onOverwrite: () => void;
}

export function SettingsConflict({
  local,
  server,
  onReload,
  onOverwrite,
}: SettingsConflictProps) {
  return (
    <div
      className="border-destructive/40 bg-destructive/5 flex flex-col gap-3 rounded-xl border p-4"
      role="alertdialog"
      aria-label="Configuration conflict"
    >
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium">Conflict — config.yaml changed on disk</p>
        <p className="text-muted-foreground text-sm">
          Someone (or another tool) edited the file while you were editing it
          here. Reload to drop your changes, or Overwrite to save yours anyway.
        </p>
      </div>
      {/* Stub: T12 swaps these <pre>s for a real diff view. */}
      <div className="grid grid-cols-2 gap-2">
        <pre className="border-border bg-muted/50 max-h-40 overflow-auto rounded border p-2 text-xs">
          {local}
        </pre>
        <pre className="border-border bg-muted/50 max-h-40 overflow-auto rounded border p-2 text-xs">
          {server}
        </pre>
      </div>
      <div className="flex gap-2">
        <Button variant="outline" size="sm" onClick={onReload}>
          Reload from disk
        </Button>
        <Button variant="destructive" size="sm" onClick={onOverwrite}>
          Overwrite anyway
        </Button>
      </div>
    </div>
  );
}
