import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ActiveImportStatus } from "@/api/useActiveImport";
import type { ArtistArtBackfillStatus } from "@/api/useArtistArt";
import type { DiskSyncStatus } from "@/api/useDiskSync";
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
  instrumental: 0,
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
  orphans_trashed: 0,
  playlists_reexported: 0,
  current: null,
  error: null,
  artist: null,
  album_id: null,
  scope_label: "library",
  failures: [],
  finished_at: null,
};
const idleDiskSync: DiskSyncStatus = {
  phase: "idle",
  job_id: null,
  total: 0,
  processed: 0,
  removed: 0,
  updated: 0,
  unchanged: 0,
  read_errors: 0,
  emptied_albums: 0,
  playlists_reexported: 0,
  current: null,
  error: null,
  failures: [],
};

let importData: ActiveImportStatus = idleImport;
let lyricsData: LyricsBackfillStatus = idleLyrics;
let artistArtData: ArtistArtBackfillStatus = idleArtistArt;
let reorganizeData: ReorganizeBackfillStatus = idleReorganize;
let diskSyncData: DiskSyncStatus = idleDiskSync;
/** Which probes `refetch` asked again, in order. */
const refetched: string[] = [];

vi.mock("@/api/useActiveImport", () => ({
  useActiveImport: () => ({ data: importData, refetch: () => refetched.push("import") }),
}));
vi.mock("@/api/useLyricsBackfill", () => ({
  useLyricsBackfillStatus: () => ({ data: lyricsData, refetch: () => refetched.push("lyrics") }),
}));
vi.mock("@/api/useArtistArt", () => ({
  useArtistArtBackfillStatus: () => ({ data: artistArtData, refetch: () => refetched.push("artist-art") }),
}));
vi.mock("@/api/useReorganize", () => ({
  useReorganizeStatus: () => ({ data: reorganizeData, refetch: () => refetched.push("reorganize") }),
}));
vi.mock("@/api/useDiskSync", () => ({
  useDiskSyncStatus: () => ({ data: diskSyncData, refetch: () => refetched.push("disk-sync") }),
}));

beforeEach(() => {
  importData = idleImport;
  lyricsData = idleLyrics;
  artistArtData = idleArtistArt;
  reorganizeData = idleReorganize;
  diskSyncData = idleDiskSync;
  refetched.length = 0;
});

describe("useLibraryJobActive", () => {
  it("is inactive when every job is idle", () => {
    const { result } = renderHook(() => useLibraryJobActive());
    const { active, label } = result.current;
    expect({ active, label }).toEqual({ active: false, label: null });
  });

  it("refetch asks every probe again", () => {
    const { result } = renderHook(() => useLibraryJobActive());
    expect(refetched).toEqual([]);
    result.current.refetch();
    expect(refetched).toEqual([
      "import",
      "lyrics",
      "artist-art",
      "reorganize",
      "disk-sync",
    ]);
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
    const { active, label } = result.current;
    expect({ active, label }).toEqual({ active: true, label: "an import" });
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

  it("flags a running disk sync", () => {
    diskSyncData = { ...idleDiskSync, phase: "running" };
    const { result } = renderHook(() => useLibraryJobActive());
    expect(result.current.active).toBe(true);
    expect(result.current.label).toMatch(/disk sync/);
  });
});
