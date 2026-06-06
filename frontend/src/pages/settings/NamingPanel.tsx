// frontend/src/pages/settings/NamingPanel.tsx
import { AlertCircle, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { useActiveImport } from "@/api/useActiveImport";
import { useApplyConfig } from "@/api/useBeetsConfig";
import {
  type NamingConfig,
  type NamingRuleInput,
  type RenderedRule,
  type ReplaceError,
  useNaming,
  usePreviewNaming,
  useSaveNaming,
} from "@/api/useNaming";
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
      <Section>
        <p className="text-muted-foreground text-sm" role="status">
          Loading naming…
        </p>
      </Section>
    );
  }
  if (isError || !data) {
    return (
      <Section>
        <p className="text-destructive text-sm" role="alert">
          Could not load naming config.
        </p>
      </Section>
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

  const preview = usePreviewNaming();
  const save = useSaveNaming();
  const apply = useApplyConfig();
  const active = useActiveImport();
  const importActive = active.data?.active ?? false;

  // Tracks the focused template input so the Insert palette writes at the caret.
  const focusedRef = useRef<HTMLInputElement | null>(null);

  const replaceDraft = useMemo(
    () =>
      replace.map((r) => ({ pattern: r.pattern, replacement: r.replacement })),
    [replace],
  );

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

  const hasReplaceErrors = replaceErrors.length > 0;

  return (
    <Section>
      <header className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">Naming</h2>
        <p className="text-muted-foreground text-sm">
          Edit how files are named (beets{" "}
          <code className="font-mono">paths</code> /{" "}
          <code className="font-mono">replace</code>) with a live preview. New
          names apply to imported files — use{" "}
          <span className="font-medium">Reorganize</span> below to rename{" "}
          existing files. Saving writes to the same config;{" "}
          <span className="font-medium">Apply</span> to load it.
        </p>
      </header>

      {conflict && (
        <div
          className="border-border bg-muted/50 flex items-center gap-3 rounded-xl border p-3 text-sm"
          role="status"
        >
          <AlertCircle
            className="text-muted-foreground size-5 shrink-0"
            aria-hidden="true"
          />
          <span className="flex-1">
            Config changed on disk — reload to get the latest.
          </span>
          <Button
            size="sm"
            variant="outline"
            onClick={() => window.location.reload()}
          >
            Reload
          </Button>
        </div>
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
                aria-label={`Custom rule query ${i + 1}`}
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
                <Trash2 className="size-4" aria-hidden="true" />
              </Button>
            </div>
            <PathRow
              label=""
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
            <Plus className="size-4" aria-hidden="true" /> Add rule
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
          disabled={save.isPending || hasReplaceErrors}
          title={
            hasReplaceErrors
              ? "Fix the invalid replace pattern before saving"
              : undefined
          }
        >
          {save.isPending ? "Saving…" : "Save naming"}
        </Button>
        <Button
          variant="outline"
          onClick={() => apply.mutate()}
          disabled={apply.isPending || importActive}
          title={
            importActive
              ? "1 import running — Apply available when it finishes"
              : undefined
          }
        >
          {apply.isPending ? "Applying…" : "Apply"}
        </Button>
        {hasReplaceErrors && (
          <p className="text-destructive text-sm">
            Invalid replace pattern — fix to save.
          </p>
        )}
      </div>
    </Section>
  );
}

function Section({ children }: { children: React.ReactNode }) {
  return (
    <section
      aria-label="Naming"
      className="border-border flex flex-col gap-3 rounded-xl border p-4"
    >
      {children}
    </section>
  );
}

function PathRow({
  label,
  name,
  value,
  onChange,
  rendered,
  focusedRef,
}: {
  label: string;
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
        aria-label={label || `template ${name}`}
        placeholder="beets path template"
        className="font-mono"
        onChange={(e) => onChange(e.target.value)}
        onFocus={(e) => (focusedRef.current = e.currentTarget)}
      />
      <p className="text-muted-foreground text-xs">
        {rendered ? (
          <>
            <span aria-hidden="true">→ </span>
            <span className="font-mono">{rendered.sample_path || "—"}</span>
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
            onMouseDown={(e) => {
              // Keep the focused input focused — mousedown fires before blur.
              e.preventDefault();
              onInsert(t.insert);
            }}
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
        return (
          <div key={row.id} className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <Input
                aria-label={`Replace pattern ${i + 1}`}
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
                <Trash2 className="size-4" aria-hidden="true" />
              </Button>
            </div>
            {err && (
              <p className="text-destructive text-xs">
                Invalid regex: {err.message}
              </p>
            )}
          </div>
        );
      })}
      <div>
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
          <Plus className="size-4" aria-hidden="true" /> Add replacement
        </Button>
      </div>
    </div>
  );
}
