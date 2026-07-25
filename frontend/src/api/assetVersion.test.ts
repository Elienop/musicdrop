import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { bumpAssetVersion, useAssetVersion } from "@/api/assetVersion";

describe("assetVersion", () => {
  it("a scoped bump moves ONLY that scope's version", () => {
    const seven = renderHook(() => useAssetVersion("album:7"));
    const eight = renderHook(() => useAssetVersion("album:8"));
    const before7 = seven.result.current;
    const before8 = eight.result.current;

    act(() => bumpAssetVersion("album:7"));

    expect(seven.result.current).not.toBe(before7);
    // The whole point of M16: installing one cover must not remount the other
    // 191 <img> elements on a browse grid.
    expect(eight.result.current).toBe(before8);
  });

  it("an UNSCOPED bump moves every scope (library-wide refresh)", () => {
    const seven = renderHook(() => useAssetVersion("album:7"));
    const eight = renderHook(() => useAssetVersion("album:8"));
    const plain = renderHook(() => useAssetVersion());
    const before = [seven.result.current, eight.result.current, plain.result.current];

    act(() => bumpAssetVersion());

    expect(seven.result.current).not.toBe(before[0]);
    expect(eight.result.current).not.toBe(before[1]);
    expect(plain.result.current).not.toBe(before[2]);
  });

  it("a scoped bump does NOT move an unscoped consumer", () => {
    const plain = renderHook(() => useAssetVersion());
    const before = plain.result.current;
    act(() => bumpAssetVersion("artist:Radiohead"));
    expect(plain.result.current).toBe(before);
  });

  it("distinct scopes with the same name share one version", () => {
    const a = renderHook(() => useAssetVersion("artist:ABBA"));
    const b = renderHook(() => useAssetVersion("artist:ABBA"));
    act(() => bumpAssetVersion("artist:ABBA"));
    expect(a.result.current).toBe(b.result.current);
  });
});
