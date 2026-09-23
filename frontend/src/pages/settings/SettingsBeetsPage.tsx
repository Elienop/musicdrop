import type { Diagnostic } from "@codemirror/lint";
import { EditorView } from "@codemirror/view";
import { useQueryClient } from "@tanstack/react-query";
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { useEffect, useMemo, useRef, useState } from "react";

import { useLibraryJobActive } from "@/api/useLibraryJobActive";
import {
  type BeetsConfigSnapshot,
  type ConfigAdvisory,
  type ConfigOpError,
  type ValidationErrorItem,
  applyRecoveryHint,
  configOnDiskMessage,
  useApplyConfig,
  useBeetsConfig,
  useSaveConfig,
  useValidateConfig,
} from "@/api/useBeetsConfig";
import {
  Error as ErrorIcon,
  Info,
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
import {
  APPLY_FALLBACK,
  saveFailureDetail,
} from "@/pages/settings/configFailureText";
import { SettingsConflict } from "@/pages/settings/SettingsConflict";
import {
  READ_ONLY_EXTENSION,
  buildExtensions,
  buildReadOnlyExtensions,
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

/**
 * Derive the page state from the in-flight mutation flags + the
 * snapshot's `apply_pending` + local `dirty`. The mutation flags take
 * priority because they describe an in-flight action — a `saving` state
 * mid-Save should not flicker back to `dirty` if the user happens to keep
 * typing during the round-trip. A draft outranks `apply_pending` (owner
 * ruling 2026-09-23, "Edit works while pending"): once it differs from the
 * file the page is in its edit state, so Save is on and Apply is off until
 * the draft is Saved or discarded.
 */
function derivePageState(
  applying: boolean,
  saving: boolean,
  applyPending: boolean,
  dirty: boolean,
): PageState {
  if (applying) return "applying";
  if (saving) return "saving";
  if (dirty) return "dirty";
  if (applyPending) return "apply_pending";
  return "clean";
}

/**
 * Focus the editor and scroll its caret into view, below the sticky topbar
 * (81px measured at widths 375 to 1920, plus CodeMirror's default margin of
 * 5). `view.focus()` alone never scrolls, and the buttons that call this sit
 * below the 500px editor. "nearest" moves nothing once the caret is 86px
 * inside the editor and the window.
 */
function focusEditor(view: EditorView | undefined) {
  if (!view) return;
  view.focus();
  view.dispatch({
    effects: EditorView.scrollIntoView(view.state.selection.main.head, {
      y: "nearest",
      yMargin: 86,
    }),
  });
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
  // The editor's own text: set by every edit and by Reload, cleared by Cancel
  // and by a read of a new file, unless an open draft differs from that file.
  // `null` means the editor shows `data.yaml_text`. It is also the editor's
  // `value`, so a read cannot replace an open draft, and a Save's own text
  // stays on screen until its re-read lands.
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
  // The validate endpoint's OTHER channel. Advisories name valid settings that
  // MusicDrop-driven imports force or discard, so they must never reach the
  // lint gutter — but the gutter's async source is the only thing that calls
  // validate, and CodeMirror's `linter()` can only return `Diagnostic[]`.
  // Lifting them into React state here (same trick as `lintErrors` above) is
  // what lets the page render them outside the editor. Mirrors the LAST
  // validate response, so a cleared advisory disappears on the next tick.
  const [advisories, setAdvisories] = useState<ConfigAdvisory[]>([]);

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
  // each other's read-only state. Keying by `data?.yaml_text` recomputes when
  // a read brings new file text. The editor does not remount then: @uiw
  // reconfigures the same view with the new list, whose compartments start
  // read-only.
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

  // A read that brings a new file version (its sha256, the only CAS token; the
  // snapshot omits mtime_ns, which overflows a JS number). Tracking the hash,
  // not the data, lets a re-read of the same file leave everything alone.
  // - It ends an Apply refusal and a Save failure, which were about the file
  //   as it was, unless that action is still in flight: its answer is still
  //   to come.
  // - With a draft that differs from the new file, the draft stays and the
  //   conflict panel offers the new file. New file text rebuilds the
  //   extensions read-only, so editing is turned back on.
  // - Otherwise the editor takes the new file and the page is clean. A draft
  //   equals it when, for one, the read lands inside the page's own Save. An
  //   open panel closes: it would offer an older file, and its Reload would
  //   pair that text with this sha.
  const prevSha = useRef<string | undefined>(data?.sha256);
  const resetApply = applyMutation.reset;
  const applyInFlight = applyMutation.isPending;
  const resetSave = save.reset;
  const saveInFlight = save.isPending;
  useEffect(() => {
    if (!data?.sha256 || data.sha256 === prevSha.current) return;
    prevSha.current = data.sha256;
    if (!applyInFlight) resetApply();
    if (!saveInFlight) resetSave();
    if (dirty && localText !== data.yaml_text) {
      setConflict({ serverDoc: data.yaml_text, sha: data.sha256 });
      editorRef.current?.view?.dispatch({
        effects: editableCompartment.reconfigure([]),
      });
      return;
    }
    setLocalText(null);
    setDirty(false);
    setConflict(null);
  }, [
    data?.sha256,
    data?.yaml_text,
    dirty,
    localText,
    applyInFlight,
    resetApply,
    saveInFlight,
    resetSave,
    editableCompartment,
  ]);

  // Extensions for the read-only "Effective config" pane. Content-independent
  // (the doc rides in via the `value` prop, which @uiw keeps synced on
  // refetch), so this memo has no deps — the array identity stays stable and
  // the pane never needlessly rebuilds.
  const effectiveExtensions = useMemo(
    () => buildReadOnlyExtensions(shadcnTheme),
    [],
  );

  if (isPending) return <Loader />;
  if (isError) return <ErrorBanner err={error} />;
  if (!data) return null;

  const pageState = derivePageState(
    applyMutation.isPending,
    save.isPending,
    data.apply_pending,
    dirty,
  );

  async function asyncSource(text: string): Promise<Diagnostic[]> {
    try {
      const result = await validate.mutateAsync({ yaml_text: text });
      // Channel 2 first, and it is deliberately NOT folded into the
      // diagnostics below: every config an advisory fires on is valid YAML
      // that both this app and beets accept, so a red gutter marker would be
      // a lie. It rides out to the banner list beside the editor instead.
      setAdvisories(result.advisories);
      const diagnostics = mapErrorsToDiagnostics(
        result.errors,
        editorRef.current,
      );
      setLintErrors(diagnostics.length);
      return diagnostics;
    } catch {
      // If the validate endpoint itself fails (network, 5xx) we DON'T want to
      // pollute the gutter with a fake "validate failed" diagnostic — the
      // user's draft might be perfectly fine. Treat as "no lint signal";
      // hard failures still surface through React Query's error path if a
      // mutation observer ever needs them. Don't gate Save on a transient
      // backend hiccup either. Advisories clear for the same reason: we no
      // longer know whether they still hold, and a stale one would claim the
      // app overrides a key the user may have just removed.
      setLintErrors(0);
      setAdvisories([]);
      return [];
    }
  }

  function handleSave() {
    if (!data) return;
    // The Save button's own predicate, because CM6's Mod-s keymap fires
    // whenever the editor has focus and bypasses the button. `dirty` page
    // state excludes an unchanged doc (a re-Save would advance mtime and light
    // the apply_pending banner), an in-flight Save and an in-flight Apply.
    // Lint errors block it as they block the button. It also does nothing
    // while the conflict panel is open: Reload and Overwrite are the choices.
    if (pageState !== "dirty" || lintErrors > 0 || conflict) return;
    // The latest action owns the one alert: a Save ends the last Apply's. No
    // Apply is in flight here, so this never drops an Apply's answer.
    applyMutation.reset();
    const text = localText ?? data.yaml_text;
    save.mutate(
      {
        yaml_text: text,
        base_sha256: data.sha256,
      },
      {
        onSuccess: () => {
          setDirty(false);
        },
        // 409 = CAS mismatch -> open the conflict panel. Every other error,
        // 422 included, shows the "Save failed" alert below. A 422 about the
        // editor text is also painted by the lint source on its next debounce
        // tick; from then on the lint line is the recovery and the alert
        // gives way. A 422 about config.yaml on disk has no lint row, so the
        // alert prints its sentence.
        onError: openConflict,
      },
    );
  }

  // Keep the latest-callback refs pointing at this render's closures. The
  // memoized extension list calls through these (see `useMemo` above), so the
  // lint debounce / Mod-s keymap always see fresh `data`/`localText`.
  asyncSourceRef.current = asyncSource;
  onSaveRef.current = handleSave;

  /** Open (or refresh) the conflict panel for a 409 on either Save path. */
  function openConflict(err: unknown) {
    const c = parseConflictBody(err);
    if (c) setConflict(c);
  }

  function handleEdit() {
    // Entering a fresh edit session clears a stale Save failure: a settled
    // error mutation keeps its error state until reset, so without this the
    // old alert would resurface the moment the doc is dirty again. An Apply
    // refusal stays: it is true of the file until a Save or a new file
    // version ends it.
    save.reset();
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
      // Restore read-only + reset the doc back to the snapshot. @uiw syncs
      // the `value` prop (`localText ?? data.yaml_text`) only once its typing
      // latch has passed; the dispatch makes the change immediate.
      view.dispatch({
        effects: editableCompartment.reconfigure(READ_ONLY_EXTENSION),
        changes: {
          from: 0,
          to: view.state.doc.length,
          insert: data.yaml_text,
        },
      });
      // Cancel is shown only beside a draft, so the click unmounts it.
      focusEditor(view);
    }
    setLocalText(null);
    setDirty(false);
    // Cancel also dismisses any conflict modal from a prior failed Save —
    // the user explicitly chose to drop their edits, so there's nothing
    // left for the diff view to resolve. The lint count is reset too; the
    // doc reset re-runs the linter, which counts the file's own rows again.
    setConflict(null);
    setLintErrors(0);
    // Discarding also clears a Save failure, whose alert shows only beside a
    // draft. An Apply refusal stays, as it does through every edit: the file
    // is the one Apply refused.
    save.reset();
  }

  function handleApply() {
    // The editor takes no edits while Apply runs (Edit opens it again after).
    // Apply is on only while the draft equals the file, so nothing is lost.
    editorRef.current?.view?.dispatch({
      effects: editableCompartment.reconfigure(READ_ONLY_EXTENSION),
    });
    // A 409 means a job this page has not seen holds the library. Ask the
    // probes again so the "Apply paused" line speaks for it, and goes when
    // the job ends.
    applyMutation.mutate(undefined, {
      onError: (err) => {
        if (err.status === 409) job.refetch();
      },
    });
  }

  function handleConflictReload() {
    if (!conflict) return;
    const view = editorRef.current?.view;
    if (view) {
      // Drop the user's local edits in the editor itself: replace its doc
      // with the panel's file, from the 409 body or from the read that
      // opened the panel. @uiw syncs the `value` prop (`localText ??
      // data.yaml_text`) only once its typing latch has passed; the dispatch
      // makes the change immediate. Also flip the editor back to read-only
      // so the page state is consistent with the cleared `dirty` flag.
      view.dispatch({
        effects: editableCompartment.reconfigure(READ_ONLY_EXTENSION),
        changes: {
          from: 0,
          to: view.state.doc.length,
          insert: conflict.serverDoc,
        },
      });
      // Before the panel's focused button unmounts, or focus drops to <body>.
      focusEditor(view);
    }
    // The editor holds the panel's file until the re-read below lands.
    setLocalText(conflict.serverDoc);
    setDirty(false);
    setConflict(null);
    setLintErrors(0);
    // Read the file again: the next Save sends the snapshot's sha. Once a read
    // with a new sha lands, the text and the sha come from one file version:
    // that read puts its own text in the editor (the effect above).
    void queryClient.invalidateQueries({ queryKey: ["beets-config"] });
  }

  function handleConflictOverwrite() {
    // No Save while an Apply is in flight, from this button either.
    if (!conflict || !data || applyMutation.isPending) return;
    const text = localText ?? data.yaml_text;
    save.mutate(
      {
        yaml_text: text,
        base_sha256: conflict.sha,
      },
      {
        // Focus goes to the editor before the panel's focused button
        // unmounts, here and on a non-409 failure.
        onSuccess: () => {
          setDirty(false);
          setConflict(null);
          focusEditor(editorRef.current?.view);
        },
        // Another writer since the first 409: the panel takes the newer file
        // and token, the same as for the first 409. Any other failure closes
        // the panel: the Save alert shows it, and Save is the way to retry.
        // Focus goes to the editor, where the draft is; the alert and Save sit
        // just below it.
        onError: (err) => {
          if (err.status === 409) {
            openConflict(err);
            return;
          }
          setConflict(null);
          focusEditor(editorRef.current?.view);
        },
      },
    );
  }

  // An Apply failure other than the library-job 409, which the "Apply paused"
  // line speaks for once the probes see the job.
  const applyFailed =
    applyMutation.isError && applyMutation.error?.status !== 409;

  return (
    <div className="flex flex-col gap-8">
      <section className="flex flex-col gap-4" aria-label="Beets configuration">
        <header className="flex flex-col gap-1">
          <SectionLabel>Beets configuration</SectionLabel>
          <p className="text-muted-foreground text-sm">
            Loaded from{" "}
            <code className="font-mono break-all">{data.config_path}</code>
          </p>
        </header>

        <ConfigStateBanner
          state={pageState}
          jobActive={job.active}
          applyFailed={applyFailed}
          data={data}
        />

        <ConfigAdvisories advisories={advisories} />

        <CodeMirror
          ref={editorRef}
          value={localText ?? data.yaml_text}
          height="500px"
          // `theme="none"` opts out of @uiw/react-codemirror's default theme so
          // our shadcnTheme variables are the only thing setting colors.
          theme="none"
          extensions={extensions}
          onChange={(value) => {
            setLocalText(value);
            // A draft edit ends the last Save's failure, whether it was about
            // the text or about config.yaml on disk: an alert hidden by the
            // lint line and shown again would be announced twice for one Save.
            if (save.isError) save.reset();
          }}
        />

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            onClick={handleEdit}
            disabled={pageState !== "clean" && pageState !== "apply_pending"}
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
              {lintErrors} validation error{lintErrors > 1 ? "s" : ""};
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

        {/* Surface Save/Apply failures — otherwise the spinner just ends and the
            banner silently returns to its resting state, so the user never
            learns the click failed. Each is co-gated on page state so a stale
            error can't outlive it: a settled mutation keeps its error until
            reset. The Apply refusal is about the file, so it shows beside a
            draft too; it stays mounted through edits and Cancel (so it is not
            announced again) until handleSave, a new file version or another
            Apply ends it. The Save failure shows only beside a draft. A 409
            on Save opens the conflict panel below; a 409 on Apply is the
            library-job gate, which the "Apply paused" line speaks for.
            Everything else is a destructive alert. The Save alert also gives
            way to the lint line, which is then the recovery. */}
        {applyFailed &&
          (pageState === "apply_pending" || pageState === "dirty") && (
          <p className="text-destructive text-sm break-words" role="alert">
            Apply failed.{" "}
            {applyRecoveryHint(applyMutation.error) ?? APPLY_FALLBACK}
          </p>
        )}
        {save.isError &&
          save.error?.status !== 409 &&
          pageState === "dirty" &&
          lintErrors === 0 && (
            <p className="text-destructive text-sm break-words" role="alert">
              Save failed.{" "}
              {saveFailureDetail(configOnDiskMessage(save.error?.body))}
            </p>
          )}

        {conflict && (
          // Keyed by the newer file's sha, from a 409 or from a read, so a
          // second 409 or a newer read remounts the panel and moves focus to
          // it, the same as the first.
          <SettingsConflict
            key={conflict.sha}
            local={localText ?? data.yaml_text}
            server={conflict.serverDoc}
            onReload={handleConflictReload}
            onOverwrite={handleConflictOverwrite}
          />
        )}
      </section>

      <section className="flex flex-col gap-4" aria-label="Effective config">
        <header className="flex flex-col gap-1">
          <SectionLabel>Effective config</SectionLabel>
          <p className="text-muted-foreground text-sm">
            The fully-merged config (beets + plugin defaults), with secrets
            redacted. Read-only &mdash; computed from your config, never saved.
          </p>
        </header>

        <CodeMirror
          value={data?.effective_yaml ?? ""}
          height="500px"
          // `theme="none"` opts out of @uiw/react-codemirror's default theme so
          // the shadcnTheme (folded into effectiveExtensions) owns the colors.
          theme="none"
          editable={false}
          readOnly
          extensions={effectiveExtensions}
        />
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
      <output
        className="text-muted-foreground flex items-center gap-2 text-sm"
      >
        <Spinner className="size-4 animate-spin" aria-hidden="true" />
        Loading configuration&hellip;
      </output>
    </section>
  );
}

/** Same error surface as the L2 page — a destructive alert with the message,
 * rather than a generic React Query error that drops the page chrome. */
function ErrorBanner({ err }: Readonly<{ err: unknown }>) {
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

/**
 * The validate endpoint's ADVISORY channel, rendered as its own surface.
 *
 * One `StatusBanner` per advisory, `tone="neutral"` — the ambient treatment
 * (`role="status"`, muted icon), NOT warning or destructive. That tone choice
 * is the whole point: an advisory fires on a setting that is perfectly valid
 * and saves cleanly, it just does not do what it reads as — a key MusicDrop
 * overrides on import, or an `include:` entry beets drops. Anything
 * louder would read as "your config is broken", which is the error channel's
 * job and is already handled by CodeMirror's red gutter.
 *
 * The `<ul>` is real semantics, not a wrapper: advisories are a list, and the
 * accessible name lets a screen reader announce how many there are before
 * reading them. The banner owns no margin (spec §4), so the `gap-2` here is
 * the caller's, and the empty case renders NOTHING at all — no heading, no
 * empty box — because "no advisories" is the unremarkable default.
 */
function ConfigAdvisories({
  advisories,
}: Readonly<{ advisories: ConfigAdvisory[] }>) {
  if (advisories.length === 0) return null;
  return (
    <ul className="flex flex-col gap-2" aria-label="Configuration advisories">
      {advisories.map((advisory) => (
        <li key={advisory.key}>
          <StatusBanner tone="neutral" icon={Info}>
            <div className="flex flex-col gap-1">
              {/* The dotted config path gets the same monospace treatment as
                  the config file path in the section header. */}
              <p className="font-mono font-medium">{advisory.key}</p>
              {/* Backend copy, verbatim — the page never paraphrases it. */}
              <p className="text-muted-foreground">{advisory.message}</p>
            </div>
          </StatusBanner>
        </li>
      ))}
    </ul>
  );
}

/** The single banner that mirrors the 5-state machine. Color + icon both carry
 * the state (a11y: never color alone). The apply_pending rail is the system
 * StatusBanner (tone=warning — the old hardcoded yellow + dark variants are
 * gone); dirty keeps its primary box, saving/applying share the neutral one. */
function ConfigStateBanner({
  state,
  jobActive,
  applyFailed,
  data,
}: Readonly<{
  state: PageState;
  jobActive: boolean;
  /** An Apply failure alert shows; its sentence carries the recovery. */
  applyFailed: boolean;
  data: BeetsConfigSnapshot;
}> ) {
  if (state === "clean") {
    return null;
  }
  if (state === "dirty") {
    return (
      <output
        className={cn(
          "flex items-start gap-3 rounded-xl border p-3 text-sm",
          "border-primary/40 bg-primary/5",
        )}
      >
        <Success
          className="text-primary-light mt-0.5 size-5 shrink-0"
          aria-hidden="true"
        />
        <span>
          <strong>Unsaved changes.</strong> Save to write to{" "}
          <code className="font-mono break-all">{data.config_path}</code>.
        </span>
      </output>
    );
  }
  if (state === "saving" || state === "applying") {
    return (
      <output
        className="border-border bg-muted/50 flex items-start gap-3 rounded-xl border p-3 text-sm"
      >
        <Spinner
          className="text-muted-foreground mt-0.5 size-5 shrink-0 animate-spin"
          aria-hidden="true"
        />
        <span>
          {state === "saving" ? "Saving configuration…" : "Reloading beets…"}
        </span>
      </output>
    );
  }
  // apply_pending — same copy the tests pin; the system banner supplies the
  // warning chrome + role="alert". Beside an Apply failure the tail goes: the
  // alert's sentence is the recovery, and "click Apply" would contradict one
  // that says to fix something first. While a job runs it goes too: the
  // "Apply paused" line under the buttons speaks for the job.
  const tail =
    applyFailed || jobActive ? "." : "; click Apply to load it into beets.";
  return (
    <StatusBanner tone="warning" icon={Warning}>
      <p>
        <strong>config.yaml is saved but not loaded yet</strong>
        {data.file_modified_at && (
          <> ({new Date(data.file_modified_at).toLocaleTimeString()})</>
        )}
        {tail}
      </p>
    </StatusBanner>
  );
}

/** Resolve `ValidationErrorItem[]` into CodeMirror `Diagnostic[]` keyed off
 * 1-based line numbers. A row whose line is null (a parse error with no
 * position, e.g. `!!bool ture`) is kept as a point at the start of line 1: it
 * counts toward the error line, gets a gutter marker and disables Save (the
 * server would refuse a Save of the text Validate rejected), without
 * underlining text that may be fine. Validate's lines come from the text it
 * was sent, so a row past the last line means the draft changed since; CM6
 * then drops the whole result, and the row counts only until the next pass. */
function mapErrorsToDiagnostics(
  errors: ValidationErrorItem[],
  ref: ReactCodeMirrorRef | null,
): Diagnostic[] {
  const view = ref?.view;
  if (!view) return [];
  const totalLines = view.state.doc.lines;
  return errors.map((e) => {
    const n = e.line;
    const placed = n != null && n >= 1 && n <= totalLines;
    const line = view.state.doc.line(placed ? n : 1);
    // Clamp `from` to the line's range. A backend column past line-end (drift
    // between the server's view and the live buffer, or a 0-based vs 1-based
    // off-by-one) would otherwise produce `from > to` and trigger CM6's
    // range invariant; capping at `line.to` degrades to a point at the line's
    // end instead of crashing the linter. An unplaced row's column belongs to no
    // line here, so it is ignored.
    const column = placed ? (e.column ?? 0) : 0;
    return {
      from: Math.min(line.from + column, line.to),
      to: placed ? line.to : line.from,
      severity: "error" as const,
      message: e.loc ? `${e.loc}: ${e.msg}` : e.msg,
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
