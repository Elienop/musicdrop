import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ActiveImportStatus } from "@/api/useActiveImport";
import type { ArtistArtBackfillStatus } from "@/api/useArtistArt";
import { useLibraryJobActive } from "@/api/useLibraryJobActive";
import type { LyricsBackfillStatus } from "@/api/useLyricsBackfill";
import type { ReorganizeBackfillStatus } from "@/api/useReorganize";

const idleImport: ActiveImportStatus = {
  active: false,
  origin: "manual",
  needs_review_count: 0,
};
const idleLyrics: LyricsBackfillStatus = {
  phase: "idle",
  job_id: null,
  total: 0,
  processed: 0,
  found: 0,
  not_found: 0,
  failed: 0,
  skipped: 0,
  current: null,
  writes_enabled: false,
  error: null,
  album_id: null,
  scope_label: "library",
};
const idleArtistArt: ArtistArtBackfillStatus = {
  phase: "idle",
  job_id: null,
  total: 0,
  processed: 0,
  written: 0,
  skipped: 0,
  failed: 0,
  current: null,
  error: null,
  artist: null,
  scope_label: "library",
};
const idleReorganize: ReorganizeBackfillStatus = {
  phase: "idle",
  job_id: null,
  scope: null,
  total: 0,
  processed: 0,
  moved: 0,
  skipped: 0,
  failed: 0,
  current: null,
  error: null,
  artist: null,
  album_id: null,
  scope_label: "library",
};

let importData: ActiveImportStatus = idleImport;
let lyricsData: LyricsBackfillStatus = idleLyrics;
let artistArtData: ArtistArtBackfillStatus = idleArtistArt;
let reorganizeData: ReorganizeBackfillStatus = idleReorganize;

vi.mock("@/api/useActiveImport", () => ({
  useActiveImport: () => ({ data: importData }),
}));
vi.mock("@/api/useLyricsBackfill", () => ({
  useLyricsBackfillStatus: () => ({ data: lyricsData }),
}));
vi.mock("@/api/useArtistArt", () => ({
  useArtistArtBackfillStatus: () => ({ data: artistArtData }),
}));
vi.mock("@/api/useReorganize", () => ({
  useReorganizeStatus: () => ({ data: reorganizeData }),
}));

beforeEach(() => {
  importData = idleImport;
  lyricsData = idleLyrics;
  artistArtData = idleArtistArt;
  reorganizeData = idleReorganize;
});

describe("useLibraryJobActive", () => {
  it("is inactive when every job is idle", () => {
    const { result } = renderHook(() => useLibraryJobActive());
    expect(result.current).toEqual({ active: false, label: null });
  });

  it("flags a running lyrics backfill (the case that 409'd Apply)", () => {
    lyricsData = { ...idleLyrics, phase: "running" };
    const { result } = renderHook(() => useLibraryJobActive());
    expect(result.current.active).toBe(true);
    expect(result.current.label).toMatch(/lyrics/);
  });

  it("flags an active import", () => {
    importData = { ...idleImport, active: true };
    const { result } = renderHook(() => useLibraryJobActive());
    expect(result.current).toEqual({ active: true, label: "an import" });
  });

  it("flags a running artist-art backfill", () => {
    artistArtData = { ...idleArtistArt, phase: "running" };
    const { result } = renderHook(() => useLibraryJobActive());
    expect(result.current.active).toBe(true);
    expect(result.current.label).toMatch(/artist-art/);
  });

  it("flags a running reorganize", () => {
    reorganizeData = { ...idleReorganize, phase: "running" };
    const { result } = renderHook(() => useLibraryJobActive());
    expect(result.current.active).toBe(true);
    expect(result.current.label).toMatch(/reorganize/);
  });
});
