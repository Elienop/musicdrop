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
    await waitFor(() =>
      expect(afterRule(editor)).toContain(
        "color-mix(in oklab, var(--ring) 70%, transparent)",
      ),
    );
  }
});
