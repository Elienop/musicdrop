import type { Diagnostic } from "@codemirror/lint";
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { AlertCircle, CheckCircle2, Loader2, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

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
  READ_ONLY_EXTENSION,
  buildExtensions,
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

/**
 * Wire format of the 409 body. FastAPI nests it under `detail` (the standard
 * HTTPException shape) — the conflict-handling endpoint includes the server's
 * fresh CAS tokens + the on-disk YAML so the page can either drop the local
 * draft (Reload) or overwrite-with-new-tokens (Overwrite anyway) without a
 * round-trip to refetch the snapshot.
 */
interface ConflictBody {
  detail: {
    current_yaml_text: string;
    current_sha256: string;
    current_snapshot: { mtime_ns: number };
  };
}

/**
 * Narrow `unknown` -> `ConflictState | null` for the 409 onError branch.
 *
 * Pulled out of the `onSave` handler so the deep-property access path is
 * checked once, in one place, instead of repeating the `body.detail.current_*`
 * cast at every call site. Returns `null` (not throws) on a malformed body so
 * a freak 409 with the wrong shape gracefully falls through to the React
 * Query default error path instead of crashing the page.
 */
function parseConflictBody(err: unknown): ConflictState | null {
  if (!isConfigOpError(err) || err.status !== 409 || !err.body) return null;
  const body = err.body as Partial<ConflictBody>;
  const detail = body.detail;
  if (
    !detail ||
    typeof detail.current_yaml_text !== "string" ||
    typeof detail.current_sha256 !== "string" ||
    !detail.current_snapshot ||
    typeof detail.current_snapshot.mtime_ns !== "number"
  ) {
    return null;
  }
  return {
    serverDoc: detail.current_yaml_text,
    sha: detail.current_sha256,
    mtime: detail.current_snapshot.mtime_ns,
  };
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

  // Resync local state whenever the snapshot's content hash advances (post-Save
  // / post-Apply React Query invalidation refetches and gets a new sha256).
  // Without this, an Apply refetch would swap CodeMirror's `value` prop but
  // leave `dirty=true` and a stale `localText` draft — the page would wedge
  // showing the editor as dirty with no actual diff against the new doc.
  // Tracking the hash (not just data) is precise: identity-equal refetches
  // (e.g. background revalidations that returned an unchanged snapshot) won't
  // clobber an in-progress edit. `mtime_ns` would work too, but sha256 catches
  // bytes-changed-mtime-preserved edits (matching the backend's CAS rule).
  const prevSha = useRef<string | undefined>(data?.sha256);
  useEffect(() => {
    if (data?.sha256 && data.sha256 !== prevSha.current) {
      prevSha.current = data.sha256;
      setLocalText(null);
      setDirty(false);
    }
  }, [data?.sha256]);

  // Latest-callback refs. The CM6 extension list is memoized (so Compartments
  // stay stable across renders), but the linter source + Mod-s handler need to
  // see the *current* `data`/`localText` on every fire — not the closures
  // captured when the memo was built. Refs let the memoized extensions call
  // through a stable indirection while always reading the latest handler.
  const asyncSourceRef = useRef<(text: string) => Promise<Diagnostic[]>>(
    async () => [],
  );
  const onSaveRef = useRef<() => void>(() => {});

  // Build the extension list (and the Compartments it owns) once per snapshot.
  // Compartments must NOT be module-level singletons: under React 19
  // StrictMode dev-mode double-mounts the first dispatch can target a
  // torn-down view, and two concurrently-mounted SettingsPages would clobber
  // each other's read-only state. Keying by `data?.yaml_text` recomputes on
  // a snapshot swap (post-Apply refetch) which is exactly when the editor
  // remounts anyway.
  const { extensions, editableCompartment } = useMemo(
    () =>
      buildExtensions({
        initialDoc: data?.yaml_text ?? "",
        // Route through refs so the memo doesn't have to re-key on every
        // render's freshly-created `asyncSource`/`handleSave` closures.
        asyncSource: (text) => asyncSourceRef.current(text),
        onSave: () => onSaveRef.current(),
        onDirtyChange: setDirty,
        theme: shadcnTheme,
      }),
    [data?.yaml_text],
  );

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
    if (!data) return;
    const text = localText ?? data.yaml_text;
    save.mutate(
      {
        yaml_text: text,
        base_mtime_ns: data.mtime_ns,
        base_sha256: data.sha256,
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
          // here. Any other status falls through (React Query exposes via
          // `save.error` if a future banner wants to surface it).
          const c = parseConflictBody(err);
          if (c) setConflict(c);
        },
      },
    );
  }

  // Keep the latest-callback refs pointing at this render's closures. The
  // memoized extension list calls through these (see `useMemo` above), so the
  // lint debounce / Mod-s keymap always see fresh `data`/`localText`.
  asyncSourceRef.current = asyncSource;
  onSaveRef.current = handleSave;

  function handleEdit() {
    const view = editorRef.current?.view;
    if (view) {
      view.dispatch({
        effects: editableCompartment.reconfigure([]),
      });
      view.focus();
    }
  }

  function handleCancel() {
    if (!data) return;
    const view = editorRef.current?.view;
    if (view) {
      // Restore read-only + reset the doc back to the snapshot. The doc reset
      // is required because `value={data.yaml_text}` on `<CodeMirror>` only
      // applies on remount; once the user has typed, CM6 owns the doc and
      // we have to dispatch the change explicitly.
      view.dispatch({
        effects: editableCompartment.reconfigure(READ_ONLY_EXTENSION),
        changes: {
          from: 0,
          to: view.state.doc.length,
          insert: data.yaml_text,
        },
      });
    }
    setLocalText(null);
    setDirty(false);
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
    if (!conflict || !data) return;
    const text = localText ?? data.yaml_text;
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
        extensions={extensions}
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
        {/* Discard the in-progress draft and return to read-only. Only visible
            while dirty so it doesn't sit next to a no-op target when clean. */}
        {pageState === "dirty" && (
          <Button variant="ghost" onClick={handleCancel}>
            Cancel
          </Button>
        )}
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
      // Clamp `from` to the line's range. A backend column past line-end (drift
      // between the server's view and the live buffer, or a 0-based vs 1-based
      // off-by-one) would otherwise produce `from > to` and trigger CM6's
      // range invariant; capping at `line.to` degrades to a whole-line mark
      // instead of crashing the linter.
      const from = Math.min(line.from + (e.column ?? 0), line.to);
      return {
        from,
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

