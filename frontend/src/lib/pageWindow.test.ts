import { describe, expect, it } from "vitest";

import { pageWindow } from "@/lib/pageWindow";

describe("pageWindow", () => {
  it("shows every page when few", () => {
    expect(pageWindow(2, 7)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });
  it("windows the middle with two gaps", () => {
    expect(pageWindow(30, 48)).toEqual([1, "gap", 28, 29, 30, 31, 32, "gap", 48]);
  });
  it("expands instead of a single-page gap", () => {
    // gap standing for exactly page 2 is silly — show the page.
    expect(pageWindow(4, 48)).toEqual([1, 2, 3, 4, 5, 6, "gap", 48]);
  });
  it("clamps at the ends", () => {
    expect(pageWindow(1, 48)).toEqual([1, 2, 3, "gap", 48]);
    expect(pageWindow(48, 48)).toEqual([1, "gap", 46, 47, 48]);
  });
  it("degenerate single page", () => {
    expect(pageWindow(1, 1)).toEqual([1]);
  });
});
