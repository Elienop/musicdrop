import type { Diagnostic } from "@codemirror/lint";
import { useQueryClient } from "@tanstack/react-query";
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { useEffect, useMemo, useRef, useState } from "react";

import { useLibraryJobActive } from "@/api/useLibraryJobActive";
import {
  type BeetsConfigSnapshot,
  type ConfigOpError,
  type ValidationErrorItem,
  useApplyConfig,
  useBeetsConfig,
  useSaveConfig,
  useValidateConfig,
} from "@/api/useBeetsConfig";
import {
  Error as ErrorIcon,
  Spinner,
  Success,
  Warning,
} from "@/components/icons";
import { SectionLabel } from "@/components/system/SectionLabel";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { DiskSyncPanel } from "./DiskSyncPanel";
import { ReorganizeLibraryPanel } from "./ReorganizeLibraryPanel";
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
}

/**
 * Wire format of the 409 body. FastAPI nests it under `detail` (the standard
 * HTTPException shape) — the conflict-handling endpoint includes the server's
 * fresh CAS token + the on-disk YAML so the page can either drop the local
 * draft (Reload) or overwrite-with-fresh-token (Overwrite anyway) without a
 * round-trip to refetch the snapshot.
 */
interface ConflictBody {
  detail: {
    current_yaml_text: string;
    current_sha256: string;
  };
}

/**
 * Narrow `unknown` -> `ConflictState | null` for the 409 onError branch.
 *
 * Returns `null` (not throws) on a malformed body so a freak 409 with the
 * wrong shape gracefully falls through to the React Query default error path
 * instead of crashing the page.
 */
function parseConflictBody(err: unknown): ConflictState | null {
  if (!isConfigOpError(err) || err.status !== 409 || !err.body) return null;
  const body = err.body as Partial<ConflictBody>;
  const detail = body.detail;
  if (
    !detail ||
    typeof detail.current_yaml_text !== "string" ||
    typeof detail.current_sha256 !== "string"
  ) {
    return null;
  }
  return {
    serverDoc: detail.current_yaml_text,
    sha: detail.current_sha256,
  };
}

export function SettingsBeetsPage() {
  const { data, isPending, isError, error } = useBeetsConfig();
  const save = useSaveConfig();
  const applyMutation = useApplyConfig();
  const validate = useValidateConfig();
  // Any library job (import / lyrics / artist-art / reorganize) blocks Apply
  // server-side; mirror that so Apply disables instead of firing into a 409.
  const job = useLibraryJobActive();
  const queryClient = useQueryClient();

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
  // Mirror of the validate endpoint's error count so the Save button can
  // disable while the editor has lint errors. The linter paints the gutter
  // marker but doesn't own button state; without this we'd let the user
  // click Save, the backend would return 422, and the failure would never
  // surface (the linter only re-fires after the debounce). Reset to 0 on
  // every successful validate so a cleared error frees the button up
  // immediately.
  const [lintErrors, setLintErrors] = useState(0);

  // Resync local state whenever the snapshot's content hash advances (post-Save
  // / post-Apply React Query invalidation refetches and gets a new sha256).
  // Without this, an Apply refetch would swap CodeMirror's `value` prop but
  // leave `dirty=true` and a stale `localText` draft — the page would wedge
  // showing the editor as dirty with no actual diff against the new doc.
  // Tracking the hash (not just data) is precise: identity-equal refetches
  // (e.g. background revalidations that returned an unchanged snapshot) won't
  // clobber an in-progress edit. sha256 is also the only CAS token — the
  // snapshot intentionally omits mtime_ns because nanosecond ints overflow
  // JavaScript's Number.MAX_SAFE_INTEGER.
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

  async function asyncSource(text: string): Promise<Diagnostic[]> {
    try {
      const errors = await validate.mutateAsync({ yaml_text: text });
      const diagnostics = mapErrorsToDiagnostics(errors, editorRef.current);
      setLintErrors(diagnostics.length);
      return diagnostics;
    } catch {
      // If the validate endpoint itself fails (network, 5xx) we DON'T want to
      // pollute the gutter with a fake "validate failed" diagnostic — the
      // user's draft might be perfectly fine. Treat as "no lint signal";
      // hard failures still surface through React Query's error path if a
      // mutation observer ever needs them. Don't gate Save on a transient
      // backend hiccup either.
      setLintErrors(0);
      return [];
    }
  }

  function handleSave() {
    if (!data) return;
    // Bail out when the page isn't in `dirty` state. CM6's Mod-s keymap fires
    // whenever the editor has focus — including read-only mode — so without
    // this guard a stray Ctrl+S would re-Save the unchanged snapshot, which
    // succeeds, advances mtime, and lights up the (misleading) apply_pending
    // banner. We also block re-firing during an in-flight Save and while a
    // conflict modal is open (the user has Reload/Overwrite to choose from,
    // not a redo-Save).
    if (!dirty || save.isPending || conflict) return;
    // Same guard as the disabled button — never fire Save while there are
    // unresolved lint errors. The button is disabled, but Mod-s would
    // otherwise bypass it.
    if (lintErrors > 0) return;
    const text = localText ?? data.yaml_text;
    save.mutate(
      {
        yaml_text: text,
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
    // Cancel also dismisses any conflict modal from a prior failed Save —
    // the user explicitly chose to drop their edits, so there's nothing
    // left for the diff view to resolve. The lint count is reset too: the
    // doc is back to the clean snapshot, which has no errors (it's what
    // beets is already running on).
    setConflict(null);
    setLintErrors(0);
  }

  function handleApply() {
    applyMutation.mutate();
  }

  function handleConflictReload() {
    if (!conflict) return;
    const view = editorRef.current?.view;
    if (view) {
      // Drop the user's local edits in the editor itself — replace its doc
      // with the fresh on-disk text that the 409 body carried back. Without
      // this dispatch the editor visually keeps the stale local edit even
      // though React state thinks we're clean (the `value={data.yaml_text}`
      // prop only applies on remount; CM6 owns the doc after the first user
      // keystroke). Also flip the editor back to read-only so the page state
      // is internally consistent with the cleared `dirty` flag.
      view.dispatch({
        effects: editableCompartment.reconfigure(READ_ONLY_EXTENSION),
        changes: {
          from: 0,
          to: view.state.doc.length,
          insert: conflict.serverDoc,
        },
      });
    }
    setLocalText(null);
    setDirty(false);
    setConflict(null);
    setLintErrors(0);
    // Refresh the snapshot so its CAS sha matches the new on-disk bytes —
    // the next Save (after a fresh Edit) sends the right base_sha256 from
    // React Query's cache instead of the stale pre-409 value.
    void queryClient.invalidateQueries({ queryKey: ["beets-config"] });
  }

  function handleConflictOverwrite() {
    if (!conflict || !data) return;
    const text = localText ?? data.yaml_text;
    save.mutate(
      {
        yaml_text: text,
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
    <div className="flex flex-col gap-8">
      <section className="flex flex-col gap-4" aria-label="Beets configuration">
        <header className="flex flex-col gap-1">
          <SectionLabel>Beets configuration</SectionLabel>
          <p className="text-muted-foreground text-sm">
            Loaded from <code className="font-mono">{data.config_path}</code>
          </p>
        </header>

        <ConfigStateBanner
          state={pageState}
          jobActive={job.active}
          data={data}
        />

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
            disabled={pageState !== "dirty" || lintErrors > 0}
          >
            {pageState === "saving" ? (
              <>
                <Spinner className="animate-spin" aria-hidden="true" />
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
          {pageState === "dirty" && lintErrors > 0 && (
            // Visible helper text — the disabled Save's reason (no title=
            // tooltip; spec §4 disabled-reason rule).
            <p className="text-destructive text-sm">
              {lintErrors} validation error{lintErrors > 1 ? "s" : ""} —
              <span className="text-muted-foreground"> fix to save.</span>
            </p>
          )}
          <Button
            onClick={handleApply}
            disabled={pageState !== "apply_pending" || job.active}
          >
            {pageState === "applying" ? (
              <>
                <Spinner className="animate-spin" aria-hidden="true" />
                Applying&hellip;
              </>
            ) : (
              "Apply changes"
            )}
          </Button>
          {pageState === "apply_pending" && job.active && (
            // Visible helper text under the disabled Apply (same rule).
            <p className="text-muted-foreground text-sm">
              Apply paused &mdash; {job.label} is running; available when it
              finishes.
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
      <ReorganizeLibraryPanel />
      <DiskSyncPanel />
    </div>
  );
}

/** Loading skeleton — matches the L1/L2 phrasing so the spinner copy stays
 * stable across the read-only -> editor refactor. */
function Loader() {
  return (
    <section className="flex flex-col gap-6" aria-label="Beets configuration">
      <div className="flex flex-col gap-1">
        <SectionLabel>Beets configuration</SectionLabel>
      </div>
      <p
        className="text-muted-foreground flex items-center gap-2 text-sm"
        role="status"
      >
        <Spinner className="size-4 animate-spin" aria-hidden="true" />
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
    <section className="flex flex-col gap-6" aria-label="Beets configuration">
      <div className="flex flex-col gap-1">
        <SectionLabel>Beets configuration</SectionLabel>
      </div>
      <div
        className="border-destructive/40 bg-destructive/5 flex items-start gap-3 rounded-xl border p-4"
        role="alert"
      >
        <ErrorIcon
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
 * the state (a11y: never color alone). The apply_pending rail is the system
 * StatusBanner (tone=warning — the old hardcoded yellow + dark variants are
 * gone); dirty keeps its primary box, saving/applying share the neutral one. */
function ConfigStateBanner({
  state,
  jobActive,
  data,
}: {
  state: PageState;
  jobActive: boolean;
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
        <Success
          className="text-primary-light mt-0.5 size-5 shrink-0"
          aria-hidden="true"
        />
        <p>
          <strong>Unsaved changes.</strong> Save to write to{" "}
          <code className="font-mono">{data.config_path}</code>.
        </p>
      </div>
    );
  }
  if (state === "saving" || state === "applying") {
    return (
      <div
        className="border-border bg-muted/50 flex items-start gap-3 rounded-xl border p-3 text-sm"
        role="status"
      >
        <Spinner
          className="text-muted-foreground mt-0.5 size-5 shrink-0 animate-spin"
          aria-hidden="true"
        />
        <p>
          {state === "saving" ? "Saving configuration…" : "Reloading beets…"}
        </p>
      </div>
    );
  }
  // apply_pending — same copy the tests pin; the system banner supplies the
  // warning chrome + role="alert".
  return (
    <StatusBanner tone="warning" icon={Warning}>
      <p>
        <strong>config.yaml is saved but not loaded yet</strong>
        {data.file_modified_at && (
          <> ({new Date(data.file_modified_at).toLocaleTimeString()})</>
        )}
        {jobActive
          ? " — Apply available once the running job finishes."
          : " — click Apply to load it into beets."}
      </p>
    </StatusBanner>
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
