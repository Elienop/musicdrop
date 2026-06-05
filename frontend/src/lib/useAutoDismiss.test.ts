import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { useAutoDismiss } from "./useAutoDismiss";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test("shows while active, then hides after ms", () => {
  const { result } = renderHook(() => useAutoDismiss(true, "job-1", 8000));
  expect(result.current).toBe(true);
  act(() => vi.advanceTimersByTime(7999));
  expect(result.current).toBe(true);
  act(() => vi.advanceTimersByTime(1));
  expect(result.current).toBe(false);
});

test("inactive never shows", () => {
  const { result } = renderHook(() => useAutoDismiss(false, "job-1", 8000));
  expect(result.current).toBe(false);
  act(() => vi.advanceTimersByTime(8000));
  expect(result.current).toBe(false);
});

test("a new resetKey re-arms the message", () => {
  const { result, rerender } = renderHook(
    ({ k }: { k: string }) => useAutoDismiss(true, k, 8000),
    { initialProps: { k: "job-1" } },
  );
  act(() => vi.advanceTimersByTime(8000));
  expect(result.current).toBe(false); // faded
  rerender({ k: "job-2" }); // a new job
  expect(result.current).toBe(true); // shows again
});
