import { AlertCircle, Loader2, TriangleAlert } from "lucide-react";

import { useBeetsConfig } from "@/api/useBeetsConfig";

/** Coarse-grained relative time for the "Loaded from … · <when>" line. Keeps
 * the page passive — no ticking clock, no library. Falls back to an absolute
 * timestamp once the snapshot is older than a day (anything older is more
 * useful as a date than as "X hours ago"). */
function formatRelative(iso: string): string {
  const then = new Date(iso);
  const diffMs = Date.now() - then.getTime();
  const mins = Math.round(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins === 1) return "1 minute ago";
  if (mins < 60) return `${mins} minutes ago`;
  const hours = Math.round(mins / 60);
  if (hours === 1) return "1 hour ago";
  if (hours < 24) return `${hours} hours ago`;
  return then.toLocaleString();
}

export function SettingsPage() {
  const { data, isPending, isError, error } = useBeetsConfig();

  return (
    <section
      className="flex max-w-4xl flex-col gap-6"
      aria-label="Beets configuration"
    >
      <div className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">
          Beets configuration
        </h2>
        {data && (
          <p className="text-muted-foreground text-sm">
            Loaded from <code className="font-mono">{data.config_path}</code>
            <span aria-hidden="true"> · </span>
            {formatRelative(data.loaded_at)}
          </p>
        )}
      </div>

      {isPending && (
        <p
          className="text-muted-foreground flex items-center gap-2 text-sm"
          role="status"
        >
          <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
          Loading configuration&hellip;
        </p>
      )}

      {isError && (
        <div
          className="border-destructive/40 bg-destructive/5 flex items-start gap-3 rounded-xl border p-4"
          role="alert"
        >
          <AlertCircle
            className="text-destructive mt-0.5 size-5 shrink-0"
            aria-hidden="true"
          />
          <div className="flex flex-col gap-1 text-sm">
            <p className="font-medium">Could not load configuration.</p>
            {(error as Error | null)?.message && (
              <p className="text-muted-foreground">
                {(error as Error).message}
              </p>
            )}
          </div>
        </div>
      )}

      {data?.restart_required && (
        <div
          className="flex items-start gap-3 rounded-xl border border-yellow-400/60 bg-yellow-50 p-4 text-sm text-yellow-900 dark:border-yellow-500/40 dark:bg-yellow-950/40 dark:text-yellow-100"
          role="alert"
        >
          <TriangleAlert
            className="mt-0.5 size-5 shrink-0 text-yellow-600 dark:text-yellow-400"
            aria-hidden="true"
          />
          <p>
            <strong>config.yaml was modified</strong>
            {data.file_modified_at && (
              <> at {new Date(data.file_modified_at).toLocaleTimeString()}</>
            )}{" "}
            &mdash; restart MusicDrop to apply the changes.
          </p>
        </div>
      )}

      {data && (
        <pre
          data-testid="config-yaml"
          className="border-border bg-muted/50 overflow-auto rounded-xl border p-4 font-mono text-sm leading-relaxed"
        >
          {data.yaml_text}
        </pre>
      )}
    </section>
  );
}
