import { yaml } from "@codemirror/lang-yaml";
import { lintGutter, linter, type Diagnostic } from "@codemirror/lint";
import { Compartment, EditorState, Prec } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { basicSetup } from "codemirror";

/**
 * Minimal shadcn-aligned CM6 theme. Pulls from the same CSS variables the rest
 * of the app uses (declared in `frontend/src/styles.css` :root + .dark), so the
 * editor inherits the project's tokens rather than hard-coding hex values.
 *
 * `dark: true` flips CM6's own dark heuristics (caret color, selection alpha)
 * — that's safe here even in light mode because we override the visible bits
 * via CSS variables; the heuristic only matters for the few properties we
 * don't override.
 */
export const shadcnTheme = EditorView.theme(
  {
    "&": {
      backgroundColor: "var(--background)",
      color: "var(--foreground)",
      fontSize: "13px",
      // The bordered container is rendered by the page; keep the editor flush
      // with it so the gutter dots don't overhang the rounded corners.
      border: "1px solid var(--border)",
      borderRadius: "var(--radius)",
    },
    ".cm-content": {
      caretColor: "var(--foreground)",
      fontFamily:
        "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace",
      padding: "12px 0",
    },
    ".cm-gutters": {
      backgroundColor: "var(--muted)",
      color: "var(--muted-foreground)",
      border: "none",
      borderRight: "1px solid var(--border)",
    },
    ".cm-activeLine": {
      backgroundColor: "color-mix(in oklab, var(--muted) 60%, transparent)",
    },
    ".cm-activeLineGutter": {
      backgroundColor: "transparent",
      color: "var(--foreground)",
    },
    ".cm-selectionBackground, .cm-content ::selection": {
      backgroundColor:
        "color-mix(in oklab, var(--primary) 24%, transparent) !important",
    },
    ".cm-cursor": {
      borderLeftColor: "var(--foreground)",
    },
    // Lint markers — keep the dot using the destructive token so an error in
    // the gutter reads the same as the page's "save failed" banner.
    ".cm-lintRange-error": {
      backgroundImage: "none",
      textDecoration: "underline wavy var(--destructive)",
      textUnderlineOffset: "3px",
    },
  },
  { dark: true },
);

/**
 * The read-only triplet — kept named so both the initial factory config AND
 * the page-level "Cancel" handler (which dispatches the same triplet to
 * restore read-only after a discard) can share it.
 *
 * Both layers are required: `EditorState.readOnly` blocks model mutation,
 * `EditorView.editable.of(false)` blocks DOM contenteditable. Neither alone
 * is enough — see CM6 reference manual under "EditorState.readOnly". The
 * `tabindex=0` keeps the read-only content focusable so keyboard users can
 * arrow-key through it before clicking Edit.
 */
export const READ_ONLY_EXTENSION = [
  EditorState.readOnly.of(true),
  EditorView.editable.of(false),
  EditorView.contentAttributes.of({ tabindex: "0" }),
];

/**
 * Canonical CM6 extension factory for the SettingsPage editor.
 *
 * Compartments are created PER CALL (not module-level): module-level
 * Compartments would be clobbered if two SettingsPages mounted at once, and
 * React 19 StrictMode dev-mode double-mounts can leave the first dispatch
 * targeting a torn-down view. Returning the Compartments alongside the
 * extension array lets the page dispatch reconfigures against the *same*
 * Compartment instance the EditorView was wired with.
 *
 * The page calls this inside `useMemo` keyed by `initialDoc`, so the
 * Compartment identity is stable across re-renders for a given doc — only
 * a fresh snapshot (post-Apply refetch) rebuilds them.
 *
 * Extension order is load-bearing:
 *   1. `basicSetup` — registers the default keymap.
 *   2. `yaml()` — language. Must be after basicSetup so its highlight overlay
 *      sits on top of basicSetup's default highlight style.
 *   3. `lintGutter()` + `linter(...)` — the gutter must be added before the
 *      linter so its dots have a place to render.
 *   4. `Prec.high(keymap...)` — our Mod-s. Promoted above basicSetup so the
 *      default Mod-s (which is a no-op in CM6 but reserved on some platforms)
 *      doesn't swallow it.
 *   5. `editableCompartment.of(...)` — read-only triplet, last so the
 *      explicit read-only state wins over any handler defaults from above.
 */
export function buildExtensions(opts: {
  initialDoc: string;
  asyncSource: (text: string) => Promise<Diagnostic[]>;
  onSave: (text: string) => void;
  onDirtyChange: (dirty: boolean) => void;
  theme: ReturnType<typeof EditorView.theme>;
}) {
  const editableCompartment = new Compartment();
  const themeCompartment = new Compartment();
  const extensions = [
    basicSetup,
    yaml(),
    lintGutter(),
    // `delay: 500` — CM6's built-in debounce. The backend validate endpoint is
    // O(parse + Pydantic), so 500ms is plenty to coalesce keystrokes without
    // feeling laggy on save.
    linter((view) => opts.asyncSource(view.state.doc.toString()), {
      delay: 500,
    }),
    Prec.high(
      keymap.of([
        {
          key: "Mod-s",
          preventDefault: true,
          run: (view) => {
            opts.onSave(view.state.doc.toString());
            return true;
          },
        },
      ]),
    ),
    editableCompartment.of(READ_ONLY_EXTENSION),
    themeCompartment.of(opts.theme),
    EditorView.updateListener.of((u) => {
      if (u.docChanged) {
        opts.onDirtyChange(u.state.doc.toString() !== opts.initialDoc);
      }
    }),
  ];
  return { extensions, editableCompartment, themeCompartment };
}
