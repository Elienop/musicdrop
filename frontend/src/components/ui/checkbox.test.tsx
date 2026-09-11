import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { Checkbox } from "@/components/ui/checkbox";

/** The `before:size-N` token on an element, in CSS px. THROWS when there is
 * none: a lookup that answers 0 or undefined turns the floor assertion below
 * into a tautology, which is how a vacuous class pin gets written. */
function pseudoSize(el: Element): number {
  const found = /(?:^|\s)before:size-([\d.]+)(?:\s|$)/.exec(el.className);
  if (found === null) throw new Error(`no before:size-* class on: ${el.className}`);
  return Number(found[1]) * 4; // Tailwind's spacing step is 0.25rem = 4px.
}

/** The drawn `size-N`, same contract. */
function drawnSize(el: Element): number {
  const found = /(?:^|\s)size-([\d.]+)(?:\s|$)/.exec(el.className);
  if (found === null) throw new Error(`no size-* class on: ${el.className}`);
  return Number(found[1]) * 4;
}

/**
 * WCAG 2.2 SC 2.5.8 (decisions 40): every caller inherits a ≥24px tap target
 * from the primitive, and the DRAWN box does not change. jsdom computes no
 * layout, so what a test can hold is which element carries which token and the
 * relationship between them; the measured 24×24 per call site is in the
 * branch's browser pass.
 */
describe("Checkbox target size", () => {
  test("the tap target is a pseudo-element of at least 24px, and the drawn box is smaller", () => {
    render(<Checkbox aria-label="Select" />);
    const box = screen.getByRole("checkbox");
    expect(pseudoSize(box)).toBeGreaterThanOrEqual(24);
    // The point of the fix: the target grows, the control does not.
    expect(drawnSize(box)).toBeLessThan(pseudoSize(box));
    expect(drawnSize(box)).toBe(16);
  });

  test("the target is centred on the control and positioned against it", () => {
    render(<Checkbox aria-label="Select" />);
    const box = screen.getByRole("checkbox");
    // `relative` is the pseudo's containing block. Without it the target is
    // laid out against some ancestor and lands somewhere else entirely.
    expect(box).toHaveClass("relative");
    for (const token of [
      "before:absolute",
      "before:top-1/2",
      "before:left-1/2",
      "before:-translate-x-1/2",
      "before:-translate-y-1/2",
    ]) {
      expect(box.className.split(/\s+/)).toContain(token);
    }
  });

  test("the pseudo-element is generated at all", () => {
    render(<Checkbox aria-label="Select" />);
    const box = screen.getByRole("checkbox");
    const before = box.className.split(/\s+/).filter((c) => c.startsWith("before:"));
    // Not vacuous: there ARE `before:` tokens, and one of them must be
    // `content`. `content` is the single token that makes a ::before generate
    // a box — without it the size and the four positioning tokens are inert,
    // the target silently reverts to the drawn 16×16, and nothing else in the
    // class string changes. jsdom renders no pseudo-elements, so this is what
    // a test can hold; the measured 24×24 per call site is in the branch's
    // browser pass.
    expect(before.length).toBeGreaterThan(0);
    expect(before.filter((c) => c.startsWith("before:content-"))).toEqual([
      "before:content-['']",
    ]);
  });

  test("the target is out of flow and paints nothing", () => {
    render(<Checkbox aria-label="Select" />);
    const box = screen.getByRole("checkbox");
    // Absolute: no row grows. No background/border token on the pseudo: the
    // enlarged area must stay invisible.
    expect(box.className.split(/\s+/)).toContain("before:absolute");
    expect(box.className).not.toMatch(/before:(bg|border|ring|shadow)-/);
  });

  test("a caller's className still wins for the drawn box", () => {
    render(<Checkbox aria-label="Select" className="ml-4" />);
    const box = screen.getByRole("checkbox");
    expect(box).toHaveClass("ml-4");
    expect(pseudoSize(box)).toBeGreaterThanOrEqual(24);
  });
});
