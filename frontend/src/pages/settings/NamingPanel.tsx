// frontend/src/pages/settings/NamingPanel.tsx
import { useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import {
  applyRecoveryHint,
  useApplyConfig,
  useBeetsConfig,
} from "@/api/useBeetsConfig";
import { useLibraryJobActive } from "@/api/useLibraryJobActive";
import {
  NAMING_KEY,
  type NamingConfig,
  type NamingDraft,
  type NamingRuleInput,
  type RenderedRule,
  type ReplaceError,
  type ReplaceRuleInput,
  useNaming,
  usePreviewNaming,
  useSaveNaming,
} from "@/api/useNaming";
import { Add, Error as ErrorIcon, Remove, Warning } from "@/components/icons";
import { SettingsSection } from "@/components/system/SettingsSection";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
  APPLY_FALLBACK,
  saveFailureDetail,
} from "@/pages/settings/configFailureText";
import { NAMING_FIELDS, NAMING_FUNCTIONS } from "@/pages/settings/namingFields";

const PREVIEW_DEBOUNCE_MS = 250;

interface CustomRow extends NamingRuleInput {
  id: number;
}
interface ReplaceRow {
  id: number;
  pattern: string;
  replacement: string;
}

let nextId = 1;
const mkId = () => nextId++;

/** Curated typographic→ASCII replace rules for the "Add recommended rules"
 * button. MusicBrainz spells names with Unicode that is visually identical to
 * ASCII in a file browser (e.g. "blink‐182" with a U+2010 HYPHEN), which mints
 * a twin folder next to the ASCII one. beets' built-in replace rules leave
 * these alone, so we offer them one click. Patterns are Python `re` and stored
 * as literal `\uXXXX` text (String.raw keeps the backslashes literal at runtime)
 * so the characters stay legible instead of being invisible glyphs.
 * Keep in sync with backend/app/beets/config.starter.yaml. */
const RECOMMENDED_REPLACE_RULES: ReplaceRuleInput[] = [
  { pattern: String.raw`[\u2010\u2011\u2212]`, replacement: "-" },
  { pattern: String.raw`[\u2013\u2014]`, replacement: "-" },
  { pattern: String.raw`[\u2018\u2019\u02bc]`, replacement: "'" },
  { pattern: String.raw`[\u201c\u201d]`, replacement: "_" },
  { pattern: String.raw`\u2026`, replacement: "..." },
];

/** beets' path-separator rule (beets/config_default.yaml, `'[\\/]': _`). Only
 * the warning's wording reads it: without it, an album with no artist or album
 * tag renders an absolute path and is filed outside the library. beets' rules
 * themselves come from the naming read, never from here. */
const PATH_SEPARATOR_PATTERN = String.raw`[\\/]`;

/** The rows "Add recommended rules" leaves: the typographic rules, then every
 * other row in its current order, then beets' own rules in beets' order. The
 * order matches the starter config: typographic rules run first so the ASCII
 * they write is cleaned like typed ASCII, and beets' rules run last so no rule
 * can write a separator or edge period past them. A row whose pattern matches a
 * rule takes that rule's slot with its own replacement (every such row, so a
 * pattern typed twice loses neither); a missing rule gets a new row. */
function withRecommendedRules(
  rows: ReplaceRow[],
  beetsRules: ReplaceRuleInput[],
): ReplaceRow[] {
  const typographic = new Set(RECOMMENDED_REPLACE_RULES.map((r) => r.pattern));
  const beets = beetsRules.filter((r) => !typographic.has(r.pattern));
  const rulePatterns = new Set([...typographic, ...beets.map((r) => r.pattern)]);
  const slot = (rule: ReplaceRuleInput): ReplaceRow[] => {
    const own = rows.filter((r) => r.pattern === rule.pattern);
    return own.length > 0 ? own : [{ id: mkId(), ...rule }];
  };
  return [
    ...RECOMMENDED_REPLACE_RULES.flatMap(slot),
    ...rows.filter((r) => !rulePatterns.has(r.pattern)),
    ...beets.flatMap(slot),
  ];
}

/** Assemble the ordered flat rule list the API expects (default, comp,
 * singleton, then custom) — mirrors the backend `assemble_rules`. */
function assemble(
  base: { default: string; comp: string; singleton: string },
  custom: NamingRuleInput[],
): NamingRuleInput[] {
  const rules: NamingRuleInput[] = [
    { query: "default", template: base.default },
    { query: "comp", template: base.comp },
    { query: "singleton", template: base.singleton },
  ];
  for (const c of custom) rules.push({ query: c.query, template: c.template });
  return rules;
}

/** The rules and replace rows Save sends. It leaves out a rule with no query
 * and a replace row with no pattern, which the backend would not write either
 * (`_naming_map`, `_replace_map`). It also leaves out a rule whose template is
 * only whitespace; the backend drops only an empty one. */
function saveBody(
  base: { default: string; comp: string; singleton: string },
  custom: NamingRuleInput[],
  replace: NamingDraft["replace"],
): NamingDraft {
  return {
    rules: assemble(base, custom).filter(
      (r) => r.query !== "" && r.template.trim() !== "",
    ),
    replace: replace
      .filter((r) => r.pattern !== "")
      .map((r) => ({ pattern: r.pattern, replacement: r.replacement })),
  };
}

export function NamingPanel() {
  const { data, isPending, isError, error, refetch } = useNaming();
  if (isPending) {
    return (
      <SettingsSection title="Naming">
        <output className="text-muted-foreground text-sm block">
          Loading naming…
        </output>
      </SettingsSection>
    );
  }
  if (isError || !data) {
    return (
      <SettingsSection title="Naming">
        {/* SettingsTrashPage's load-error recipe; its comment measures why the
          * alert needs `w-full` under `items-start`. */}
        <div className="flex flex-col items-start gap-2">
          <div role="alert" className="flex w-full max-w-prose flex-col gap-1">
            <p className="text-destructive text-sm">
              Could not load naming config.
            </p>
            {error?.onDisk && (
              <p className="text-muted-foreground text-sm break-words">
                {error.onDisk}
              </p>
            )}
          </div>
          <Button variant="outline" size="sm" onClick={() => void refetch()}>
            Try again
          </Button>
        </div>
      </SettingsSection>
    );
  }
  // Remount on a fresh snapshot (post-save/apply) so the editable state reseeds.
  return <NamingEditor key={data.sha256} initial={data} />;
}

function NamingEditor({ initial }: Readonly<{ initial: NamingConfig }>) {
  const [base, setBase] = useState({
    default: initial.default ?? "",
    comp: initial.comp ?? "",
    singleton: initial.singleton ?? "",
  });
  const [custom, setCustom] = useState<CustomRow[]>(
    initial.custom.map((c) => ({ ...c, id: mkId() })),
  );
  const [replace, setReplace] = useState<ReplaceRow[]>(
    initial.replace.map((r) => ({ ...r, id: mkId() })),
  );
  const [previews, setPreviews] = useState<RenderedRule[]>(initial.previews);
  const [replaceErrors, setReplaceErrors] = useState<ReplaceError[]>(
    initial.replace_errors,
  );
  const [conflict, setConflict] = useState(false);

  const qc = useQueryClient();
  const preview = usePreviewNaming();
  const save = useSaveNaming();
  const apply = useApplyConfig();
  // Any library job (import / lyrics / artist-art / reorganize backfill) blocks
  // Apply server-side (it tears down + rebuilds beets). Mirror that gate here so
  // the button is disabled — not clickable into a 409.
  const job = useLibraryJobActive();
  // Read apply-pending from the shared config snapshot (queryKey ["beets-config"]),
  // so the "Saved — now Apply" cue survives this panel's post-save remount and
  // clears automatically once Apply reloads beets.
  const config = useBeetsConfig();
  const applyPending = config.data?.apply_pending ?? false;

  // Every draft edit goes through these, and ends the last Save's failure,
  // whether it was about the draft or about config.yaml on disk: an alert
  // hidden by the replace line and shown again would be announced twice for
  // one Save.
  function draftSetter<T>(
    set: React.Dispatch<React.SetStateAction<T>>,
  ): React.Dispatch<React.SetStateAction<T>> {
    return (value) => {
      if (save.isError) save.reset();
      set(value);
    };
  }
  const updateBase = draftSetter(setBase);
  const updateCustom = draftSetter(setCustom);
  const updateReplace = draftSetter(setReplace);

  // Tracks the focused template input so the Insert palette writes at the caret.
  const focusedRef = useRef<HTMLInputElement | null>(null);

  const replaceDraft = useMemo(
    () =>
      replace.map((r) => ({ pattern: r.pattern, replacement: r.replacement })),
    [replace],
  );

  // Whether a Save would send something other than the on-disk snapshot. It
  // gates Save (a re-Save would advance mtime and re-light the apply-pending
  // cue) and Apply. It compares what Save sends, not the rows: a Save of rows
  // the backend drops writes the same bytes, and a same-sha Save does not
  // remount the panel, so a row-level flag would stay set with Apply off.
  // The panel remounts on a fresh sha256, so `initial` is always the current
  // on-disk config.
  const dirty = useMemo(() => {
    const cur = JSON.stringify(saveBody(base, custom, replaceDraft));
    const init = JSON.stringify(
      saveBody(
        {
          default: initial.default ?? "",
          comp: initial.comp ?? "",
          singleton: initial.singleton ?? "",
        },
        initial.custom,
        initial.replace,
      ),
    );
    return cur !== init;
  }, [base, custom, replaceDraft, initial]);

  const previewMutate = preview.mutate;
  // Debounced live preview for every change.
  useEffect(() => {
    const rules = assemble(base, custom);
    const id = setTimeout(() => {
      previewMutate(
        { rules, replace: replaceDraft },
        {
          onSuccess: (res) => {
            setPreviews(res.rendered);
            setReplaceErrors(res.replace_errors);
          },
        },
      );
    }, PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [base, custom, replaceDraft, previewMutate]);

  // previews[] aligns with assemble()'s order: 0=default, 1=comp, 2=singleton,
  // then custom in order.
  const rendered = (i: number): RenderedRule | undefined => previews[i];

  function applyValue(name: string, value: string) {
    if (name === "default" || name === "comp" || name === "singleton") {
      updateBase((b) => ({ ...b, [name]: value }));
    } else if (name.startsWith("custom-tmpl-")) {
      const id = Number(name.slice("custom-tmpl-".length));
      updateCustom((rows) =>
        rows.map((r) => (r.id === id ? { ...r, template: value } : r)),
      );
    }
  }

  // Insert a token into the last-focused template input. Wired to `onClick` (not
  // `onMouseDown`) so KEYBOARD activation (Enter/Space) works too — `focusedRef`
  // still points at the input the user came from. The chip's `onMouseDown`
  // preventDefault keeps the caret for the mouse path.
  function insertToken(token: string) {
    const el = focusedRef.current;
    if (!el) return;
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? el.value.length;
    const next = el.value.slice(0, start) + token + el.value.slice(end);
    applyValue(el.name, next);
    requestAnimationFrame(() => {
      el.focus();
      const caret = start + token.length;
      el.setSelectionRange(caret, caret);
    });
  }

  // One alert at a time, the latest action's: each click ends the other's
  // failure. Neither fires while the other is in flight (the buttons say so),
  // so a reset never drops a pending result.
  function handleSave() {
    setConflict(false);
    apply.reset();
    save.mutate(
      {
        ...saveBody(base, custom, replaceDraft),
        base_sha256: initial.sha256,
      },
      {
        onError: (e) => {
          if (e.status === 409) setConflict(true);
        },
      },
    );
  }

  function handleApply() {
    save.reset();
    // A 409 means a job this panel has not seen holds the library. Ask the
    // probes again so the "Apply paused" line speaks for it, and goes when
    // the job ends.
    apply.mutate(undefined, {
      onError: (err) => {
        if (err.status === 409) job.refetch();
      },
    });
  }

  function reloadFromDisk() {
    setConflict(false);
    // Reseed THIS panel from the on-disk values (a fresh sha remounts the
    // editor) — far less destructive than reloading the whole window.
    void qc.invalidateQueries({ queryKey: NAMING_KEY });
    void qc.invalidateQueries({ queryKey: ["beets-config"] });
  }

  const hasReplaceErrors = replaceErrors.length > 0;
  // A save error other than the 409 conflict (which has its own banner): a 422
  // from a template/regex the preview missed or about config.yaml on disk, a
  // 500, or a network failure.
  const saveError = save.isError && save.error?.status !== 409;
  // An Apply failure other than the library-job 409, which the "Apply paused"
  // line speaks for once the probes see the job.
  const applyFailed = apply.isError && apply.error?.status !== 409;

  return (
    <SettingsSection title="Naming">
      <p className="text-muted-foreground text-sm">
        Edit how files are named (beets <code className="font-mono">paths</code>{" "}
        / <code className="font-mono">replace</code>) with a live preview. New
        names apply to imported files; use{" "}
        <span className="font-medium">Reorganize library</span> (Settings →
        Beets) to rename existing files. Saving writes to the same config;{" "}
        <span className="font-medium">Apply</span> to load it. Leave a field
        blank to use beets&rsquo; built-in default.
      </p>

      {conflict && (
        <StatusBanner
          tone="destructive"
          icon={ErrorIcon}
          action={
            <Button size="sm" variant="outline" onClick={reloadFromDisk}>
              Reload
            </Button>
          }
        >
          Config changed on disk; your save was refused. Reload to load the
          on-disk version (your unsaved edits here will be discarded).
        </StatusBanner>
      )}

      <div className="flex flex-col gap-4">
        <PathRow
          label="Default"
          name="default"
          value={base.default}
          onChange={(v) => updateBase((b) => ({ ...b, default: v }))}
          rendered={rendered(0)}
          focusedRef={focusedRef}
        />
        <PathRow
          label="Compilations"
          name="comp"
          value={base.comp}
          onChange={(v) => updateBase((b) => ({ ...b, comp: v }))}
          rendered={rendered(1)}
          focusedRef={focusedRef}
        />
        <PathRow
          label="Singletons"
          name="singleton"
          value={base.singleton}
          onChange={(v) => updateBase((b) => ({ ...b, singleton: v }))}
          rendered={rendered(2)}
          focusedRef={focusedRef}
        />

        {custom.map((row, i) => (
          <div
            key={row.id}
            className="border-border/60 flex flex-col gap-2 border-l-2 pl-3"
          >
            <div className="flex items-center gap-2">
              <Input
                aria-label={`Custom rule ${i + 1} query`}
                placeholder="query (e.g. albumtype:soundtrack)"
                value={row.query}
                onChange={(e) =>
                  updateCustom((rows) =>
                    rows.map((r) =>
                      r.id === row.id ? { ...r, query: e.target.value } : r,
                    ),
                  )
                }
                className="max-w-xs font-mono"
              />
              <Button
                variant="ghost"
                size="icon"
                aria-label={`Remove custom rule ${i + 1}`}
                onClick={() =>
                  updateCustom((rows) => rows.filter((r) => r.id !== row.id))
                }
              >
                <Remove className="size-4" aria-hidden="true" />
              </Button>
            </div>
            <PathRow
              label=""
              ariaLabel={`Custom rule ${i + 1} template`}
              name={`custom-tmpl-${row.id}`}
              value={row.template}
              onChange={(v) =>
                updateCustom((rows) =>
                  rows.map((r) =>
                    r.id === row.id ? { ...r, template: v } : r,
                  ),
                )
              }
              rendered={rendered(3 + i)}
              focusedRef={focusedRef}
            />
          </div>
        ))}

        <div>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              updateCustom((rows) => [
                ...rows,
                { id: mkId(), query: "", template: "" },
              ])
            }
          >
            <Add className="size-4" aria-hidden="true" /> Add rule
          </Button>
        </div>
      </div>

      <InsertPalette onInsert={insertToken} />

      <ReplaceEditor
        rows={replace}
        setRows={updateReplace}
        errors={replaceErrors}
        beetsRules={initial.beets_replace}
      />

      <div className="border-border mt-2 flex flex-wrap items-center gap-3 border-t pt-3">
        <Button
          onClick={handleSave}
          disabled={
            save.isPending || apply.isPending || hasReplaceErrors || !dirty
          }
        >
          {save.isPending ? "Saving…" : "Save naming"}
        </Button>
        {/* Apply loads the file on disk, so it waits while a draft differs
            from it, the same as on the Beets page. */}
        <Button
          variant="outline"
          onClick={handleApply}
          disabled={
            apply.isPending ||
            save.isPending ||
            job.active ||
            !applyPending ||
            dirty
          }
        >
          {apply.isPending ? "Applying…" : "Apply"}
        </Button>
        {hasReplaceErrors && (
          <p className="text-destructive text-sm">
            Invalid replace pattern. Fix to save.
          </p>
        )}
        {/* Why Apply is off beside a draft. Gives way to the replace line
            and the conflict banner, which name another step, and goes once a
            Save is sent. It shows beside an Apply failure, whose recovery
            does not contradict it. */}
        {dirty && save.isIdle && !hasReplaceErrors && !conflict && (
            <output className="text-muted-foreground text-sm block">
              Unsaved changes. Save, then Apply.
            </output>
          )}
        {/* "Saved. Click Apply" only while that is the next step: not beside
            a draft Apply would not load, and not while Apply runs. */}
        {!hasReplaceErrors &&
          applyPending &&
          !dirty &&
          !save.isPending &&
          !apply.isPending &&
          !saveError &&
          !applyFailed &&
          !conflict &&
          !job.active && (
            <output className="text-muted-foreground text-sm block">
              Saved. Click <span className="font-medium">Apply</span> to load
              it.
            </output>
          )}
        {/* Only when the file is waiting to be applied: beside a draft,
            "available when it finishes" would be false. */}
        {applyPending && job.active && !dirty && (
          <output className="text-muted-foreground text-sm block">
            Apply paused: {job.label} is running; available when it finishes.
          </output>
        )}
      </div>

      {/* While a replace pattern is invalid, its own line is the recovery. */}
      {saveError && !hasReplaceErrors && (
        <p className="text-destructive text-sm break-words" role="alert">
          Save failed. {saveFailureDetail(save.error?.onDisk)}
        </p>
      )}
      {applyFailed && (
        <p className="text-destructive text-sm break-words" role="alert">
          Apply failed. {applyRecoveryHint(apply.error) ?? APPLY_FALLBACK}
        </p>
      )}
    </SettingsSection>
  );
}

function PathRow({
  label,
  ariaLabel,
  name,
  value,
  onChange,
  rendered,
  focusedRef,
}: Readonly<{
  label: string;
  ariaLabel?: string;
  name: string;
  value: string;
  onChange: (v: string) => void;
  rendered: RenderedRule | undefined;
  focusedRef: React.RefObject<HTMLInputElement | null>;
}> ) {
  return (
    <div className="flex flex-col gap-1">
      {label && (
        <label htmlFor={`naming-${name}`} className="text-sm font-medium">
          {label}
        </label>
      )}
      <Input
        id={`naming-${name}`}
        name={name}
        value={value}
        aria-label={label || ariaLabel}
        placeholder="beets path template"
        className="font-mono"
        onChange={(e) => onChange(e.target.value)}
        onFocus={(e) => (focusedRef.current = e.currentTarget)}
      />
      <p className="text-muted-foreground text-xs">
        {rendered ? (
          <>
            <span className="sr-only">Preview: </span>
            <span aria-hidden="true">→ </span>
            <span className="font-mono">{rendered.sample_path || "-"}</span>
            {rendered.sample_source && (
              <span className="text-muted-foreground/70">
                {" "}
                (from: {rendered.sample_source})
              </span>
            )}
          </>
        ) : (
          <span className="text-muted-foreground/70">
            type a template to preview
          </span>
        )}
      </p>
    </div>
  );
}

function InsertPalette({ onInsert }: Readonly<{ onInsert: (token: string) => void }>) {
  return (
    <details className="text-sm">
      <summary className="cursor-pointer font-medium">
        Insert field / function
      </summary>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {[...NAMING_FIELDS, ...NAMING_FUNCTIONS].map((t) => (
          <button
            key={t.insert}
            type="button"
            title={t.hint}
            // Mouse: preventDefault on mousedown keeps the input caret (mousedown
            // fires before blur). Keyboard + mouse both trigger the insert via
            // onClick, so Enter/Space on a focused chip works.
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => onInsert(t.insert)}
            className="border-border hover:bg-muted rounded-md border px-2 py-0.5 font-mono text-xs"
          >
            {t.label}
          </button>
        ))}
      </div>
    </details>
  );
}

function ReplaceEditor({
  rows,
  setRows,
  errors,
  beetsRules,
}: Readonly<{
  rows: ReplaceRow[];
  setRows: React.Dispatch<React.SetStateAction<ReplaceRow[]>>;
  errors: ReplaceError[];
  /** beets' own rules, from the naming read; empty when it could not read them. */
  beetsRules: ReplaceRuleInput[];
}> ) {
  const errorAt = (i: number) => errors.find((e) => e.index === i);

  // Compared by exact pattern, like the button: a row with beets' pattern and
  // another replacement is the user's choice, not a missing rule.
  const present = new Set(rows.map((r) => r.pattern));
  const missing = beetsRules.filter((r) => !present.has(r.pattern));
  const separatorMissing = missing.some(
    (r) => r.pattern === PATH_SEPARATOR_PATTERN,
  );

  // The rows a press found complete. The "already in place" line shows until
  // the rows change, so a press that changes nothing is never a dead click.
  // `seq` counts those presses: the line's text is keyed by it, so a repeat
  // press gets new text nodes and is announced again.
  const [complete, setComplete] = useState<{
    rows: ReplaceRow[];
    seq: number;
  } | null>(null);
  const nothingToAdd = complete !== null && complete.rows === rows;

  function addRecommended() {
    const next = withRecommendedRules(rows, beetsRules);
    if (next.length === rows.length && next.every((r, i) => r === rows[i])) {
      // Not a draft edit: Save stays off and a Save failure stays shown.
      setComplete((c) => ({ rows, seq: (c?.seq ?? 0) + 1 }));
      return;
    }
    setRows(next);
  }

  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-sm font-medium">Replace characters</h3>
      {rows.map((row, i) => {
        const err = errorAt(i);
        const errId = `replace-err-${row.id}`;
        return (
          <div key={row.id} className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <Input
                aria-label={`Replace pattern ${i + 1}`}
                aria-invalid={err ? true : undefined}
                aria-describedby={err ? errId : undefined}
                placeholder="pattern (regex)"
                value={row.pattern}
                onChange={(e) =>
                  setRows((rs) =>
                    rs.map((r) =>
                      r.id === row.id ? { ...r, pattern: e.target.value } : r,
                    ),
                  )
                }
                className="max-w-[12rem] font-mono"
              />
              <span aria-hidden="true">→</span>
              <Input
                aria-label={`Replace value ${i + 1}`}
                placeholder="replacement"
                value={row.replacement}
                onChange={(e) =>
                  setRows((rs) =>
                    rs.map((r) =>
                      r.id === row.id
                        ? { ...r, replacement: e.target.value }
                        : r,
                    ),
                  )
                }
                className="max-w-[12rem] font-mono"
              />
              <Button
                variant="ghost"
                size="icon"
                aria-label={`Remove replace rule ${i + 1}`}
                onClick={() =>
                  setRows((rs) => rs.filter((r) => r.id !== row.id))
                }
              >
                <Remove className="size-4" aria-hidden="true" />
              </Button>
            </div>
            {err && (
              <p id={errId} role="alert" className="text-destructive text-xs">
                Invalid regex: {err.message}
              </p>
            )}
          </div>
        );
      })}
      {/* Beside the button it names. Only the separator rule's absence can
          file an album outside the library, so only then does it say so. */}
      {missing.length > 0 && (
        <StatusBanner tone="warning" icon={Warning}>
          <p>
            Some of beets&rsquo; own replace rules are missing
            {separatorMissing &&
              ", so an album with no artist or album tag can be filed outside your library"}
            . <span className="font-medium">Add recommended rules</span> puts
            them back.
          </p>
        </StatusBanner>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() =>
            setRows((rs) => [
              ...rs,
              { id: mkId(), pattern: "", replacement: "" },
            ])
          }
        >
          <Add className="size-4" aria-hidden="true" /> Add replacement
        </Button>
        <Button
          variant="outline"
          size="sm"
          title={
            "Maps look-alike typographic characters (‐ – — ’ “ …) " +
            "to ASCII, then restores beets’ own rules"
          }
          onClick={addRecommended}
        >
          <Add className="size-4" aria-hidden="true" /> Add recommended rules
        </Button>
        {/* Mounted empty so the line is announced when it fills; sr-only while
            empty keeps it out of the row's flex gaps. */}
        <output
          className={cn(
            "text-muted-foreground text-sm",
            !nothingToAdd && "sr-only",
          )}
        >
          {nothingToAdd && (
            <Fragment key={complete.seq}>
              Recommended rules are already in place.
            </Fragment>
          )}
        </output>
      </div>
      <p className="text-muted-foreground text-xs">
        Recommended rules map look-alike typographic characters (curly quotes,
        en/em dashes, the non-breaking hyphen, ellipsis) to ASCII so metadata
        can&rsquo;t mint visually-identical twin folders, then restore
        beets&rsquo; own rules.
      </p>
    </div>
  );
}
