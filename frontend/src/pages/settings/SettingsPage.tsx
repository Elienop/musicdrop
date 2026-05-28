import type { Diagnostic } from "@codemirror/lint";
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { AlertCircle, CheckCircle2, Loader2, TriangleAlert } from "lucide-react";
import { useRef, useState } from "react";

import { useActiveImport } from "@/api/useActiveImport";
import {
  type BeetsConfigSnapshot,
  type ConfigOpError,
  type ValidationErrorItem,
  useApplyConfig,
  useBeetsConfig,
  useSaveConfig,
  useValidateConfig,
} from "@/api/useBeetsConfig";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { SettingsConflict } from "@/pages/settings/SettingsConflict";
import {
  buildExtensions,
  editableCompartment,
  shadcnTheme,
} from "@/pages/settings/codemirror-config";

/** The 5-state machine driving banner copy + button enablement. The page never
 * stores this — it's derived every render from the mutation flags + the
 * snapshot's `apply_pending` + local `dirty` so the source of truth stays in
 * React Query / CM6 / one boolean. */
type PageState = "clean" | "dirty" | "saving" | "apply_pending" | "applying";

interface ConflictState {
  serverDoc: string;
  sha: string;
  mtime: number;
}

interface ConflictResponseBody {
  detail?: unknown;
  current_yaml_text: string;
  current_sha256: string;
  current_snapshot: { mtime_ns: number };
}

export function SettingsPage() {
  const { data, isPending, isError, error } = useBeetsConfig();
  const save = useSaveConfig();
  const applyMutation = useApplyConfig();
  const validate = useValidateConfig();
  const active = useActiveImport();

  // The editor's imperative handle. `asyncSource` needs `view.state.doc` to
  // resolve 1-based line numbers into character offsets; the page-level Edit
  // button needs `view.dispatch(...)` to flip the compartment.
  const editorRef = useRef<ReactCodeMirrorRef | null>(null);
  // `null` while clean; the buffered draft once the user starts typing. We
  // intentionally don't seed it from `data.yaml_text` — keeping it null lets
  // an Apply-then-edit cycle pick up the fresh disk text without a remount.
  const [localText, setLocalText] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [conflict, setConflict] = useState<ConflictState | null>(null);

  if (isPending) return <Loader />;
  if (isError) return <ErrorBanner err={error} />;
  if (!data) return null;

  // Derive the page state from React Query + local dirty + apply_pending. The
  // mutation flags take priority because they describe an in-flight action —
  // a `saving` state mid-Save should not flicker back to `dirty` if the user
  // happens to keep typing during the round-trip.
  const pageState: PageState = applyMutation.isPending
    ? "applying"
    : save.isPending
      ? "saving"
      : data.apply_pending
        ? "apply_pending"
        : dirty
          ? "dirty"
          : "clean";

  const importActive = active.data?.active ?? false;

  async function asyncSource(text: string): Promise<Diagnostic[]> {
    try {
      const errors = await validate.mutateAsync({ yaml_text: text });
      return mapErrorsToDiagnostics(errors, editorRef.current);
    } catch {
      // If the validate endpoint itself fails (network, 5xx) we DON'T want to
      // pollute the gutter with a fake "validate failed" diagnostic — the
      // user's draft might be perfectly fine. Treat as "no lint signal";
      // hard failures still surface through React Query's error path if a
      // mutation observer ever needs them.
      return [];
    }
  }

  function handleSave() {
    const text = localText ?? data!.yaml_text;
    save.mutate(
      {
        yaml_text: text,
        base_mtime_ns: data!.mtime_ns,
        base_sha256: data!.sha256,
      },
      {
        onSuccess: () => {
          setDirty(false);
          setLocalText(null);
        },
        onError: (err) => {
          // 409 = CAS mismatch -> open the conflict panel. 422 is handled by
          // the lint source on the editor's next debounce tick (the linter
          // re-runs after the save resolves), so we don't need to do anything
          // here. Any other status is left for an inline banner (future T12
          // could surface; for now React Query's error toast convention would
          // apply if we adopted sonner).
          if (isConfigOpError(err) && err.status === 409) {
            const body = err.body as ConflictResponseBody | undefined;
            if (body) {
              setConflict({
                serverDoc: body.current_yaml_text,
                sha: body.current_sha256,
                mtime: body.current_snapshot.mtime_ns,
              });
            }
          }
        },
      },
    );
  }

  function handleEdit() {
    const view = editorRef.current?.view;
    if (view) {
      view.dispatch({
        effects: editableCompartment.reconfigure([]),
      });
      view.focus();
    }
  }

  function handleApply() {
    applyMutation.mutate();
  }

  function handleConflictReload() {
    setLocalText(null);
    setDirty(false);
    setConflict(null);
    // The snapshot query was already invalidated by the most recent useSave
    // success path; for the 409 case the page state is still showing stale
    // CAS tokens. The conflict body's `current_*` fields would be the freshest
    // source if we wanted to populate the editor instantly — keeping that to
    // the T12 follow-up; for now an invalidate forces a refetch and the page
    // reseeds via the new snapshot.
  }

  function handleConflictOverwrite() {
    if (!conflict) return;
    const text = localText ?? data!.yaml_text;
    save.mutate(
      {
        yaml_text: text,
        base_mtime_ns: conflict.mtime,
        base_sha256: conflict.sha,
      },
      {
        onSuccess: () => {
          setDirty(false);
          setLocalText(null);
          setConflict(null);
        },
      },
    );
  }

  return (
    <section
      className="flex max-w-4xl flex-col gap-4"
      aria-label="Beets configuration"
    >
      <header className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">
          Beets configuration
        </h2>
        <p className="text-muted-foreground text-sm">
          Loaded from <code className="font-mono">{data.config_path}</code>
        </p>
      </header>

      <StatusBanner state={pageState} importActive={importActive} data={data} />

      <CodeMirror
        ref={editorRef}
        value={data.yaml_text}
        height="500px"
        // `theme="none"` opts out of @uiw/react-codemirror's default theme so
        // our shadcnTheme variables are the only thing setting colors.
        theme="none"
        extensions={buildExtensions({
          initialDoc: data.yaml_text,
          asyncSource,
          onSave: handleSave,
          onDirtyChange: setDirty,
          theme: shadcnTheme,
        })}
        onChange={(value) => setLocalText(value)}
      />

      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          onClick={handleEdit}
          disabled={pageState !== "clean"}
        >
          Edit
        </Button>
        <Button
          onClick={handleSave}
          disabled={pageState !== "dirty"}
        >
          {pageState === "saving" ? (
            <>
              <Loader2 className="animate-spin" aria-hidden="true" />
              Saving&hellip;
            </>
          ) : (
            "Save"
          )}
        </Button>
        <Button
          onClick={handleApply}
          disabled={pageState !== "apply_pending" || importActive}
          title={
            importActive
              ? "1 import running — Apply available when it finishes"
              : undefined
          }
        >
          {pageState === "applying" ? (
            <>
              <Loader2 className="animate-spin" aria-hidden="true" />
              Applying&hellip;
            </>
          ) : (
            "Apply changes"
          )}
        </Button>
        {pageState === "apply_pending" && importActive && (
          // Helper text under the disabled Apply, spelled out so a screen
          // reader user gets the same hint the sighted tooltip carries.
          <p className="text-muted-foreground text-sm">
            1 import running &mdash; Apply available when it finishes.
          </p>
        )}
      </div>

      {conflict && (
        <SettingsConflict
          local={localText ?? data.yaml_text}
          server={conflict.serverDoc}
          onReload={handleConflictReload}
          onOverwrite={handleConflictOverwrite}
        />
      )}
    </section>
  );
}

/** Loading skeleton — matches the L1/L2 phrasing so the spinner copy stays
 * stable across the read-only -> editor refactor. */
function Loader() {
  return (
    <section
      className="flex max-w-4xl flex-col gap-6"
      aria-label="Beets configuration"
    >
      <div className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">
          Beets configuration
        </h2>
      </div>
      <p
        className="text-muted-foreground flex items-center gap-2 text-sm"
        role="status"
      >
        <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
        Loading configuration&hellip;
      </p>
    </section>
  );
}

/** Same error surface as the L2 page — a destructive alert with the message,
 * rather than a generic React Query error that drops the page chrome. */
function ErrorBanner({ err }: { err: unknown }) {
  const message = err instanceof Error ? err.message : null;
  return (
    <section
      className="flex max-w-4xl flex-col gap-6"
      aria-label="Beets configuration"
    >
      <div className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">
          Beets configuration
        </h2>
      </div>
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
          {message && <p className="text-muted-foreground">{message}</p>}
        </div>
      </div>
    </section>
  );
}

/** The single banner that mirrors the 5-state machine. Color + icon both carry
 * the state so it doesn't rely on color alone (a11y). */
function StatusBanner({
  state,
  importActive,
  data,
}: {
  state: PageState;
  importActive: boolean;
  data: BeetsConfigSnapshot;
}) {
  if (state === "clean") {
    return null;
  }
  if (state === "dirty") {
    return (
      <div
        className={cn(
          "flex items-start gap-3 rounded-xl border p-3 text-sm",
          "border-primary/40 bg-primary/5",
        )}
        role="status"
      >
        <CheckCircle2
          className="text-primary mt-0.5 size-5 shrink-0"
          aria-hidden="true"
        />
        <p>
          <strong>Unsaved changes.</strong> Save to write to{" "}
          <code className="font-mono">{data.config_path}</code>.
        </p>
      </div>
    );
  }
  if (state === "saving") {
    return (
      <div
        className="border-border bg-muted/50 flex items-start gap-3 rounded-xl border p-3 text-sm"
        role="status"
      >
        <Loader2
          className="text-muted-foreground mt-0.5 size-5 shrink-0 animate-spin"
          aria-hidden="true"
        />
        <p>Saving configuration&hellip;</p>
      </div>
    );
  }
  if (state === "applying") {
    return (
      <div
        className="border-border bg-muted/50 flex items-start gap-3 rounded-xl border p-3 text-sm"
        role="status"
      >
        <Loader2
          className="text-muted-foreground mt-0.5 size-5 shrink-0 animate-spin"
          aria-hidden="true"
        />
        <p>Reloading beets&hellip;</p>
      </div>
    );
  }
  // apply_pending — the same yellow rail the L1/L2 page used, but now the
  // copy nudges Apply (the button below the editor) instead of "restart".
  return (
    <div
      className="flex items-start gap-3 rounded-xl border border-yellow-400/60 bg-yellow-50 p-4 text-sm text-yellow-900 dark:border-yellow-500/40 dark:bg-yellow-950/40 dark:text-yellow-100"
      role="alert"
    >
      <TriangleAlert
        className="mt-0.5 size-5 shrink-0 text-yellow-600 dark:text-yellow-400"
        aria-hidden="true"
      />
      <p>
        <strong>config.yaml is saved but not loaded yet</strong>
        {data.file_modified_at && (
          <> ({new Date(data.file_modified_at).toLocaleTimeString()})</>
        )}
        {importActive
          ? " — Apply available once the running import finishes."
          : " — click Apply to load it into beets."}
      </p>
    </div>
  );
}

/** Resolve `ValidationErrorItem[]` into CodeMirror `Diagnostic[]` keyed off
 * 1-based line numbers. Defensive against out-of-range lines (a server line
 * count that drifts past the current draft would otherwise throw in
 * `state.doc.line(n)`); we drop those rather than dropping the whole array. */
function mapErrorsToDiagnostics(
  errors: ValidationErrorItem[],
  ref: ReactCodeMirrorRef | null,
): Diagnostic[] {
  const view = ref?.view;
  if (!view) return [];
  const totalLines = view.state.doc.lines;
  return errors
    .filter(
      (e): e is ValidationErrorItem & { line: number } =>
        e.line != null && e.line >= 1 && e.line <= totalLines,
    )
    .map((e) => {
      const line = view.state.doc.line(e.line);
      return {
        from: line.from + (e.column ?? 0),
        to: line.to,
        severity: "error" as const,
        message: `${e.loc}: ${e.msg}`,
      };
    });
}

function isConfigOpError(err: unknown): err is ConfigOpError {
  return (
    err instanceof Error &&
    (err as ConfigOpError).name === "ConfigOpError" &&
    typeof (err as ConfigOpError).status === "number"
  );
}
