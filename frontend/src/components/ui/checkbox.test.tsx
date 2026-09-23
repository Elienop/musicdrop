import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { badgeVariants } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/utils";

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
    // `size-5` CONFLICTS with the primitive's `size-4`; twMerge keeps the
    // caller's. (`ml-4` proved only that an unrelated class survives.)
    render(<Checkbox aria-label="Select" className="size-5" />);
    const box = screen.getByRole("checkbox");
    expect(box).toHaveClass("size-5");
    expect(box).not.toHaveClass("size-4");
    expect(drawnSize(box)).toBe(20);
    expect(pseudoSize(box)).toBeGreaterThanOrEqual(24);
  });
});

/**
 * The focus-ring ALPHA, across every primitive and call site that draws one.
 *
 * Nothing pinned this before, and that is how the destructive variant shipped
 * a 1.29:1 indicator: `button.tsx` carried the violet ring at /70 in its base
 * string while the `destructive` variant re-declared the same property with
 * the destructive hue at /20. Both are the same tailwind-merge group under the
 * same modifier, so the variant DELETED the base ring rather than tinting it —
 * and the sweep that raised /50 to /70 skipped it silently.
 *
 * The class names above are DESCRIBED rather than spelled: Tailwind scans this
 * file, so a literal utility in a comment emits a real (and here unreachable)
 * rule into dist. Verified — spelling it out added a dead
 * `.focus-visible\:ring-destructive\/20` rule that matched no element.
 *
 * Measured against `--background`, composited: /20 = 1.29:1, /50 = 2.57:1,
 * /70 = 3.80:1 (destructive) and 4.02:1 (violet). WCAG 2.2 SC 1.4.11 wants 3:1
 * for a non-text indicator, and these controls declare no `border` utility —
 * preflight zeroes border-width, so the sibling `focus-visible:border-ring`
 * paints nothing and the ring is the WHOLE indicator. The worst declared
 * ground is `--muted`/`--secondary`/`--accent` at oklch(26.9%): /70 = 3.46:1
 * there, /50 = 2.43:1. So /50 clears 3:1 on no surface at all.
 *
 * Read as SOURCE BYTES through `?raw`, not by rendering: one
 * `npx shadcn@latest add button` restores the canonical `/50` string and lint,
 * typecheck, the suite and the build all stay green. `?raw` rather than
 * `node:fs` for the reason `api/gateExemptions.test.ts` documents —
 * `tsconfig.app.json` declares no `"node"` types.
 *
 * `aria-invalid:ring-destructive/20` is deliberately NOT covered: it is a state
 * tint backed by a full-opacity `aria-invalid:border-destructive`, not a focus
 * indicator. The regex keys on the `focus-visible:` modifier for that reason.
 */
const SOURCES = import.meta.glob("/src/**/*.{ts,tsx,css}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const FOCUS_RING = /focus-visible:ring-(?:ring|destructive)\/(\d+)/g;

/** Every focus-ring alpha declared in app source, with the file that holds it. */
function focusRingAlphas(): { where: string; alpha: number }[] {
  const found: { where: string; alpha: number }[] = [];
  for (const [path, src] of Object.entries(SOURCES)) {
    if (path.includes(".test.")) continue; // this file's own literals
    for (const m of src.matchAll(FOCUS_RING)) {
      found.push({ where: path, alpha: Number(m[1]) });
    }
    // styles.css's `.focus-ring` utility applies the ring without the modifier.
    for (const m of src.matchAll(/@apply ring-\[3px\] ring-ring\/(\d+)/g)) {
      found.push({ where: path, alpha: Number(m[1]) });
    }
  }
  return found;
}

describe("focus ring contrast", () => {
  test("every focus ring in the app is drawn at /70, never shadcn's /50", () => {
    const rings = focusRingAlphas();
    // Control: a sweep that matched nothing would pass vacuously, which is the
    // failure mode this test exists to prevent. The four ui/ primitives plus
    // styles.css plus the hand-rolled selects were 13 sites when written.
    expect(rings.length).toBeGreaterThanOrEqual(10);
    expect(
      rings.filter((r) => r.alpha !== 70).map((r) => `${r.where} @ /${r.alpha}`),
    ).toEqual([]);
  });

  test("the destructive variants keep a focus ring after tailwind-merge", () => {
    // Through `cn()`, which is what button.tsx and badge.tsx actually do —
    // reading the cva source would MISS the merge that deleted the base ring.
    for (const merged of [
      cn(buttonVariants({ variant: "destructive" })),
      cn(badgeVariants({ variant: "destructive" })),
    ]) {
      const alphas = [...merged.matchAll(FOCUS_RING)].map((m) => Number(m[1]));
      // Exactly one survives the merge — and it is the accessible one.
      expect(alphas).toEqual([70]);
    }
  });
});
