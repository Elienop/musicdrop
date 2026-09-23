import { EditorView } from "@codemirror/view";
import { render, screen, waitFor } from "@testing-library/react";
import { expect, test } from "vitest";

import { SettingsConflict } from "@/pages/settings/SettingsConflict";

function renderPanel() {
  render(
    <SettingsConflict
      local={"directory: /mine\n"}
      server={"directory: /disk\n"}
      onReload={() => {}}
      onOverwrite={() => {}}
    />,
  );
}

/** The CSS text of the `::after` rule CodeMirror has mounted for `editor`
 * in its current state, if any. */
function afterRule(editor: Element): string | undefined {
  for (const sheet of Array.from(document.styleSheets)) {
    for (const rule of Array.from(sheet.cssRules)) {
      if (!(rule instanceof CSSStyleRule)) continue;
      const [base, pseudo] = rule.selectorText.split("::");
      if (pseudo === "after" && editor.matches(base)) return rule.style.cssText;
    }
  }
  return undefined;
}

test("each pane is named with the panel's own words", () => {
  renderPanel();
  expect(
    screen.getByRole("textbox", { name: "Your edits" }).textContent,
  ).toBe("directory: /mine");
  expect(
    screen.getByRole("textbox", { name: "On-disk version" }).textContent,
  ).toBe("directory: /disk");
});

/** The ring, whole: without `content`, `position` or `inset` it is not
 * drawn, and without `pointer-events: none` it takes the pane's clicks (the
 * "N unchanged lines" bar opens only on a click). */
const RING =
  'content: ""; position: absolute; inset: 0px; pointer-events: none; ' +
  "box-shadow: inset 0 0 0 3px color-mix(in oklab, var(--ring) 70%, transparent);";

function paneEditors(): HTMLElement[] {
  return ["Your edits", "On-disk version"].map((name) => {
    const editor = screen.getByRole("textbox", { name }).closest(".cm-editor");
    if (!(editor instanceof HTMLElement)) throw new Error("no .cm-editor");
    return editor;
  });
}

test("a focused pane draws a ring in the focus colour, and only then", async () => {
  // Browser-verify visibility; jsdom proves only the rule applies.
  renderPanel();
  for (const name of ["Your edits", "On-disk version"]) {
    const pane = screen.getByRole("textbox", { name });
    const editor = pane.closest(".cm-editor");
    if (!editor) throw new Error("pane has no .cm-editor");
    expect(afterRule(editor)).toBeUndefined();
    // CodeMirror sets `cm-focused` after the focus event, not during it.
    pane.focus();
    await waitFor(() => expect(afterRule(editor)).toBe(RING));
  }
});

test("each pane's change markers sit clear of its ring", () => {
  renderPanel();
  const ring = Number(/inset 0 0 0 (\d+)px/.exec(RING)?.[1]);
  for (const editor of paneEditors()) {
    const gutter = editor.querySelector(".cm-changeGutter");
    if (!gutter) throw new Error("pane has no change gutter");
    const style = getComputedStyle(gutter);
    const start = Number.parseFloat(style.paddingLeft);
    // The markers start past the ring and keep upstream's 2px width.
    expect(start).toBeGreaterThan(ring);
    expect(Number.parseFloat(style.width) - start).toBe(2);
  }
});

test("the panes use CodeMirror's dark variant", () => {
  renderPanel();
  for (const editor of paneEditors()) {
    expect(
      EditorView.findFromDOM(editor)?.state.facet(EditorView.darkTheme),
    ).toBe(true);
  }
});
