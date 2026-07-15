// frontend/src/pages/settings/NamingPanel.tsx
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { useApplyConfig, useBeetsConfig } from "@/api/useBeetsConfig";
import { useLibraryJobActive } from "@/api/useLibraryJobActive";
import {
  NAMING_KEY,
  type NamingConfig,
  type NamingRuleInput,
  type RenderedRule,
  type ReplaceError,
  useNaming,
  usePreviewNaming,
  useSaveNaming,
} from "@/api/useNaming";
import { Add, Error as ErrorIcon, Remove } from "@/components/icons";
import { SettingsSection } from "@/components/system/SettingsSection";
import { StatusBanner } from "@/components/system/StatusBanner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
 * as literal `\uXXXX` text (the `\\u` here escapes to a backslash at runtime)
 * so the characters stay legible instead of being invisible glyphs.
 * Keep in sync with backend/app/beets/config.starter.yaml. */
const RECOMMENDED_REPLACE_RULES: { pattern: string; replacement: string }[] = [
  { pattern: "[\\u2010\\u2011\\u2212]", replacement: "-" },
  { pattern: "[\\u2013\\u2014]", replacement: "-" },
  { pattern: "[\\u2018\\u2019\\u02bc]", replacement: "'" },
  { pattern: "[\\u201c\\u201d]", replacement: "_" },
  { pattern: "\\u2026", replacement: "..." },
];

/** Assemble the ordered flat rule list the API expects (default, comp,
 * singleton, then custom) — mirrors the backend `assemble_rules`. */
function assemble(
  base: { default: string; comp: string; singleton: string },
  custom: CustomRow[],
): NamingRuleInput[] {
  const rules: NamingRuleInput[] = [
    { query: "default", template: base.default },
    { query: "comp", template: base.comp },
    { query: "singleton", template: base.singleton },
  ];
  for (const c of custom) rules.push({ query: c.query, template: c.template });
  return rules;
}

export function NamingPanel() {
  const { data, isPending, isError } = useNaming();
  if (isPending) {
    return (
      <SettingsSection title="Naming">
        <p className="text-muted-foreground text-sm" role="status">
          Loading naming…
        </p>
      </SettingsSection>
    );
  }
  if (isError || !data) {
    return (
      <SettingsSection title="Naming">
        <p className="text-destructive text-sm" role="alert">
          Could not load naming config.
        </p>
      </SettingsSection>
    );
  }
  // Remount on a fresh snapshot (post-save/apply) so the editable state reseeds.
  return <NamingEditor key={data.sha256} initial={data} />;
}

function NamingEditor({ initial }: { initial: NamingConfig }) {
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

  // Tracks the focused template input so the Insert palette writes at the caret.
  const focusedRef = useRef<HTMLInputElement | null>(null);

  const replaceDraft = useMemo(
    () =>
      replace.map((r) => ({ pattern: r.pattern, replacement: r.replacement })),
    [replace],
  );

  // Whether the editable state differs from the on-disk snapshot — gates Save so
  // an unchanged config can't be re-saved (which would needlessly advance mtime
  // and re-light the apply-pending cue). The panel remounts on a fresh sha256,
  // so `initial` is always the current on-disk config.
  const dirty = useMemo(() => {
    const cur = JSON.stringify({
      base,
      custom: custom.map((c) => ({ query: c.query, template: c.template })),
      replace: replaceDraft,
    });
    const init = JSON.stringify({
      base: {
        default: initial.default ?? "",
        comp: initial.comp ?? "",
        singleton: initial.singleton ?? "",
      },
      custom: initial.custom.map((c) => ({
        query: c.query,
        template: c.template,
      })),
      replace: initial.replace.map((r) => ({
        pattern: r.pattern,
        replacement: r.replacement,
      })),
    });
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
      setBase((b) => ({ ...b, [name]: value }));
    } else if (name.startsWith("custom-tmpl-")) {
      const id = Number(name.slice("custom-tmpl-".length));
      setCustom((rows) =>
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

  function handleSave() {
    setConflict(false);
    const rules = assemble(base, custom).filter(
      (r) => r.template.trim() !== "",
    );
    save.mutate(
      {
        rules,
        replace: replaceDraft.filter((r) => r.pattern !== ""),
        base_sha256: initial.sha256,
      },
      {
        onError: (e) => {
          if (e.status === 409) setConflict(true);
        },
      },
    );
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
  // from a template/regex the preview missed, a 500, or a network failure.
  const saveError = save.isError && save.error?.status !== 409;

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
          onChange={(v) => setBase((b) => ({ ...b, default: v }))}
          rendered={rendered(0)}
          focusedRef={focusedRef}
        />
        <PathRow
          label="Compilations"
          name="comp"
          value={base.comp}
          onChange={(v) => setBase((b) => ({ ...b, comp: v }))}
          rendered={rendered(1)}
          focusedRef={focusedRef}
        />
        <PathRow
          label="Singletons"
          name="singleton"
          value={base.singleton}
          onChange={(v) => setBase((b) => ({ ...b, singleton: v }))}
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
                  setCustom((rows) =>
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
                  setCustom((rows) => rows.filter((r) => r.id !== row.id))
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
                setCustom((rows) =>
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
              setCustom((rows) => [
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
        setRows={setReplace}
        errors={replaceErrors}
      />

      <div className="border-border mt-2 flex flex-wrap items-center gap-3 border-t pt-3">
        <Button
          onClick={handleSave}
          disabled={save.isPending || hasReplaceErrors || !dirty}
        >
          {save.isPending ? "Saving…" : "Save naming"}
        </Button>
        <Button
          variant="outline"
          onClick={() => apply.mutate()}
          disabled={apply.isPending || job.active || !applyPending}
        >
          {apply.isPending ? "Applying…" : "Apply"}
        </Button>
        {hasReplaceErrors && (
          <p className="text-destructive text-sm">
            Invalid replace pattern. Fix to save.
          </p>
        )}
        {!hasReplaceErrors &&
          applyPending &&
          !save.isPending &&
          !job.active && (
            <p className="text-muted-foreground text-sm" role="status">
              Saved. Click <span className="font-medium">Apply</span> to load
              it.
            </p>
          )}
        {job.active && (
          <p className="text-muted-foreground text-sm" role="status">
            Apply paused: {job.label} is running; available when it finishes.
          </p>
        )}
      </div>

      {saveError && (
        <p className="text-destructive text-sm" role="alert">
          Save failed: {save.error?.message ?? "unknown error"}
        </p>
      )}
      {apply.isError &&
        (apply.error?.status === 409 ? (
          <p className="text-muted-foreground text-sm" role="status">
            A library job is running; Apply will be available when it finishes.
          </p>
        ) : (
          <p className="text-destructive text-sm" role="alert">
            Apply failed. Your config is saved on disk; try again or restart
            MusicDrop.
          </p>
        ))}
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
}: {
  label: string;
  ariaLabel?: string;
  name: string;
  value: string;
  onChange: (v: string) => void;
  rendered: RenderedRule | undefined;
  focusedRef: React.MutableRefObject<HTMLInputElement | null>;
}) {
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

function InsertPalette({ onInsert }: { onInsert: (token: string) => void }) {
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
}: {
  rows: ReplaceRow[];
  setRows: React.Dispatch<React.SetStateAction<ReplaceRow[]>>;
  errors: ReplaceError[];
}) {
  const errorAt = (i: number) => errors.find((e) => e.index === i);
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
            "to ASCII so they can’t create twin folders"
          }
          onClick={() =>
            setRows((rs) => {
              // Dedupe by exact pattern string — a user may already have added
              // one of these by hand; only append the ones not present yet.
              const present = new Set(rs.map((r) => r.pattern));
              const additions = RECOMMENDED_REPLACE_RULES.filter(
                (r) => !present.has(r.pattern),
              ).map((r) => ({
                id: mkId(),
                pattern: r.pattern,
                replacement: r.replacement,
              }));
              return [...rs, ...additions];
            })
          }
        >
          <Add className="size-4" aria-hidden="true" /> Add recommended rules
        </Button>
      </div>
      <p className="text-muted-foreground text-xs">
        Recommended rules map look-alike typographic characters (curly quotes,
        en/em dashes, the non-breaking hyphen, ellipsis) to ASCII so metadata
        can&rsquo;t mint visually-identical twin folders.
      </p>
    </div>
  );
}
