import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

import {
  RECENT_FOLDERS_KEY,
  recentDayLabel,
  useRecentFolders,
} from "@/lib/useRecentFolders";

describe("recentDayLabel", () => {
  // Local times, so the calendar day is this browser's, whatever its zone.
  const now = new Date(2026, 8, 26, 1, 0);

  test.each<[string, Date, string]>([
    ["earlier today", new Date(2026, 8, 26, 0, 5), "Today"],
    // Two hours ago, but on the calendar day before: Yesterday, not Today.
    ["late last night", new Date(2026, 8, 25, 23, 0), "Yesterday"],
    ["three days back", new Date(2026, 8, 23, 12, 0), "3 days ago"],
    ["six days back", new Date(2026, 8, 20, 12, 0), "6 days ago"],
  ])("%s reads %s", (_, at, label) => {
    expect(recentDayLabel(at.getTime(), now)).toBe(label);
  });

  test("a week back and older, the date", () => {
    const at = new Date(2026, 8, 19, 12, 0);
    expect(recentDayLabel(at.getTime(), now)).toBe(
      at.toLocaleDateString(undefined, { dateStyle: "medium" }),
    );
  });
});

describe("useRecentFolders", () => {
  beforeEach(() => {
    localStorage.removeItem(RECENT_FOLDERS_KEY);
  });

  test("foreign or repeated entries are dropped, never shown", () => {
    localStorage.setItem(
      RECENT_FOLDERS_KEY,
      JSON.stringify([
        { path: "/a", at: 1 },
        { path: "/a", at: 2 },
        { path: "", at: 3 },
        { path: "/b" },
        { path: 4, at: 4 },
        "/c",
        null,
        { path: "/d", at: 5 },
      ]),
    );
    const { result } = renderHook(() => useRecentFolders());
    expect(result.current.recent).toEqual([
      { path: "/a", at: 1 },
      { path: "/d", at: 5 },
    ]);
  });

  test("a stored list longer than ten reads as its first ten", () => {
    const eleven = Array.from({ length: 11 }, (_, i) => ({
      path: `/p${i}`,
      at: i,
    }));
    localStorage.setItem(RECENT_FOLDERS_KEY, JSON.stringify(eleven));
    const { result } = renderHook(() => useRecentFolders());
    expect(result.current.recent).toEqual(eleven.slice(0, 10));
  });

  test("a read stops at ten rows, however long the stored list", () => {
    // A row past the tenth that counts its reads: a scan that walks the whole
    // list (10k planted rows took 71 ms a mount) touches it.
    let reads = 0;
    const planted: unknown[] = Array.from({ length: 10 }, (_, i) => ({
      path: `/p${i}`,
      at: i,
    }));
    planted.push({
      get path() {
        reads += 1;
        return "/eleventh";
      },
      at: 10,
    });
    const parse = JSON.parse.bind(JSON);
    const spy = vi
      .spyOn(JSON, "parse")
      .mockImplementation((text: string) =>
        text === "planted" ? planted : parse(text),
      );
    localStorage.setItem(RECENT_FOLDERS_KEY, "planted");
    try {
      const { result } = renderHook(() => useRecentFolders());
      expect(result.current.recent).toHaveLength(10);
      expect(reads).toBe(0);
    } finally {
      spy.mockRestore();
    }
  });

  test("text that is not a list reads as none", () => {
    localStorage.setItem(RECENT_FOLDERS_KEY, "{not json");
    const { result } = renderHook(() => useRecentFolders());
    expect(result.current.recent).toEqual([]);
  });

  test("storage that refuses a write (full) keeps the list as it was", () => {
    const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    const full = {
      getItem: () => JSON.stringify([{ path: "/kept", at: 1 }]),
      setItem: () => {
        throw new DOMException("The quota has been exceeded.", "QuotaExceededError");
      },
    };
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: full,
    });
    try {
      const { result } = renderHook(() => useRecentFolders());
      act(() => result.current.add("/new"));
      act(() => result.current.remove("/kept"));
      expect(result.current.recent).toEqual([{ path: "/kept", at: 1 }]);
    } finally {
      if (original !== undefined) {
        Object.defineProperty(globalThis, "localStorage", original);
      }
    }
  });

  test("add keeps another tab's rows", () => {
    const { result } = renderHook(() => useRecentFolders());
    // Another tab adds after this page read the list.
    localStorage.setItem(
      RECENT_FOLDERS_KEY,
      JSON.stringify([{ path: "/other", at: 1 }]),
    );
    act(() => result.current.add("/mine"));
    expect(result.current.recent.map((row) => row.path)).toEqual([
      "/mine",
      "/other",
    ]);
  });
});
