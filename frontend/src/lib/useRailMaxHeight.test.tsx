import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRailMaxHeight } from "@/lib/useRailMaxHeight";

function Probe() {
  const ref = useRef<HTMLDivElement>(null);
  useRailMaxHeight(ref);
  return <div data-testid="rail" ref={ref} />;
}

function stubViewport({ matches, height }: { matches: boolean; height: number }) {
  vi.spyOn(window, "matchMedia").mockReturnValue({
    matches,
    addEventListener: () => {},
    removeEventListener: () => {},
  } as unknown as MediaQueryList);
  Object.defineProperty(window, "innerHeight", {
    value: height,
    configurable: true,
    writable: true,
  });
}

function stubRailTop(top: number) {
  vi.spyOn(HTMLDivElement.prototype, "getBoundingClientRect").mockReturnValue({
    top,
  } as DOMRect);
}

describe("useRailMaxHeight", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it.each([
    [
      "pre-pin: sizes the rail to end at the viewport bottom gap",
      true, // header still in view — rail starts low
      180,
      // 800 - 180 - 24 = 596
      "596px",
    ],
    [
      "never sizes past the pinned offset (top clamps at 96px)",
      true, // transiently above the pin point — clamp
      60,
      // 800 - max(60, 96) - 24 = 680
      "680px",
    ],
    [
      "below md: leaves the inline style alone (mobile max-h-72 rules)",
      false,
      180,
      "",
    ],
  ])("%s", (_name, matches, railTop, expected) => {
    stubViewport({ matches, height: 800 });
    stubRailTop(railTop);
    render(<Probe />);
    expect(screen.getByTestId("rail").style.maxHeight).toBe(expected);
  });

  it("re-measures on window scroll", async () => {
    stubViewport({ matches: true, height: 800 });
    const rectSpy = vi
      .spyOn(HTMLDivElement.prototype, "getBoundingClientRect")
      .mockReturnValue({ top: 180 } as DOMRect);
    render(<Probe />);
    expect(screen.getByTestId("rail").style.maxHeight).toBe("596px");
    rectSpy.mockReturnValue({ top: 96 } as DOMRect); // now pinned
    fireEvent.scroll(window);
    await waitFor(() =>
      expect(screen.getByTestId("rail").style.maxHeight).toBe("680px"),
    );
  });
});
