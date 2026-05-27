import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useThrottledValue } from "@/lib/useThrottledValue";

describe("useThrottledValue", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  test("emits the first value immediately (leading edge)", () => {
    const { result } = renderHook(() => useThrottledValue("a", 5000));
    expect(result.current).toBe("a");
  });

  test("coalesces rapid changes, then settles on the latest", () => {
    const { result, rerender } = renderHook(
      ({ v }) => useThrottledValue(v, 5000),
      { initialProps: { v: "a" } },
    );
    expect(result.current).toBe("a");

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    rerender({ v: "b" });
    rerender({ v: "c" });
    expect(result.current).toBe("a"); // still within the window

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe("c"); // trailing edge emits the LATEST
  });
});
