import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { vi } from "vitest";

import type { AcquisitionQueueStatus } from "@/api/useAcquisitionStatus";
import type { ActiveImportStatus } from "@/api/useActiveImport";
import type { ArtistArtBackfillStatus } from "@/api/useArtistArt";
import type { LyricsBackfillStatus } from "@/api/useLyricsBackfill";
import type { ReorganizeBackfillStatus } from "@/api/useReorganize";
import { useActivity, useActivityDismissals } from "@/api/useActivity";

const idleImport: ActiveImportStatus = {
  active: false,
  origin: "manual",
  needs_review_count: 0,
};
const idleAcquisition: AcquisitionQueueStatus = {
  phase: "idle", queued: 0, current: null, processed: 0,
  set_aside: 0, failed: 0, error: null, inbox_pending: 0,
};
const idleLyrics: LyricsBackfillStatus = {
  phase: "idle", job_id: null, total: 0, processed: 0, found: 0,
  not_found: 0, failed: 0, skipped: 0, current: null,
  writes_enabled: false, error: null, album_id: null, scope_label: "library",
};
const idleArtistArt: ArtistArtBackfillStatus = {
  phase: "idle", job_id: null, total: 0, processed: 0, written: 0,
  skipped: 0, failed: 0, current: null, error: null, artist: null,
  scope_label: "library",
};
const idleReorganize: ReorganizeBackfillStatus = {
  phase: "idle", job_id: null, scope: null, total: 0, processed: 0,
  moved: 0, skipped: 0, failed: 0, orphans_trashed: 0, current: null, error: null,
  artist: null, album_id: null, scope_label: "library",
};

let importData: ActiveImportStatus = idleImport;
let acquisitionData: AcquisitionQueueStatus = idleAcquisition;
let lyricsData: LyricsBackfillStatus = idleLyrics;
let artistArtData: ArtistArtBackfillStatus = idleArtistArt;
let reorganizeData: ReorganizeBackfillStatus = idleReorganize;

vi.mock("@/api/useActiveImport", () => ({
  useActiveImport: () => ({ data: importData }),
}));
vi.mock("@/api/useAcquisitionStatus", () => ({
  useAcquisitionStatus: () => ({ data: acquisitionData }),
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

function renderActivity() {
  return renderHook(() => ({
    activity: useActivity(),
    dismissals: useActivityDismissals(),
  }));
}

describe("useActivity", () => {
  beforeEach(() => {
    importData = idleImport;
    acquisitionData = idleAcquisition;
    lyricsData = idleLyrics;
    artistArtData = idleArtistArt;
    reorganizeData = idleReorganize;
    sessionStorage.clear();
  });

  it("returns no rows and zero running when everything is idle", () => {
    const { result } = renderActivity();
    expect(result.current.activity.rows).toEqual([]);
    expect(result.current.activity.runningCount).toBe(0);
  });

  it("maps a running manual import to a running row with a job deep link", () => {
    importData = {
      active: true, job_id: "j1", origin: "manual", needs_review_count: 2,
    };
    const { result } = renderActivity();
    expect(result.current.activity.rows).toEqual([
      {
        id: "import:j1",
        kind: "import",
        label: "Import",
        state: "running",
        countsText: "2 awaiting review",
        href: "/import?job=j1",
      },
    ]);
    expect(result.current.activity.runningCount).toBe(1);
  });

  it("a sweep-origin import renders the sweep row with counters", () => {
    importData = {
      active: true,
      job_id: "s1",
      origin: "sweep",
      needs_review_count: 0,
      sweep: {
        processed: 412,
        auto_applied: 268,
        banked: 144,
        skipped_known: 9,
        current_folder: "/library/Adele",
        paused: false,
      },
    };
    const { result } = renderActivity();
    const row = result.current.activity.rows.find((r) => r.kind === "import");
    expect(row?.label).toBe("Sweep & bank");
    expect(row?.scope).toBe("/library/Adele");
    expect(row?.state).toBe("running");
    expect(row?.countsText).toBe("412 processed · 144 banked");
    expect(row?.href).toBe("/import?job=s1");
  });

  it("suppresses the import row for inbox-origin imports — the acquisition row owns it", () => {
    importData = {
      active: true, job_id: "j2", origin: "inbox", needs_review_count: 0,
    };
    acquisitionData = {
      ...idleAcquisition, phase: "running", queued: 3, current: "Drop One",
    };
    const { result } = renderActivity();
    expect(result.current.activity.rows).toEqual([
      {
        id: "acquisition:queue",
        kind: "acquisition",
        label: "Importing from inbox",
        scope: "Drop One",
        state: "running",
        countsText: "3 queued",
        href: "/review",
      },
    ]);
    expect(result.current.activity.runningCount).toBe(1);
  });

  it("maps a running library lyrics backfill with progress, and an album-scoped fetch with an album link", () => {
    lyricsData = {
      ...idleLyrics, phase: "running", job_id: "L1", processed: 3, total: 10,
    };
    const { result, rerender } = renderActivity();
    expect(result.current.activity.rows).toEqual([
      {
        id: "lyrics:L1",
        kind: "lyrics",
        label: "Lyrics backfill",
        scope: "library",
        state: "running",
        progress: { done: 3, total: 10 },
        href: "/settings/metadata",
      },
    ]);

    lyricsData = {
      ...idleLyrics, phase: "running", job_id: "L2", processed: 1, total: 9,
      album_id: 7, scope_label: "Radiohead — In Rainbows",
    };
    rerender();
    expect(result.current.activity.rows[0]).toMatchObject({
      id: "lyrics:L2",
      label: "Fetching lyrics",
      scope: "Radiohead — In Rainbows",
      href: "/albums/7",
    });
  });

  it("keeps terminal done rows while the hook reports them, with nonzero outcome counts", () => {
    lyricsData = {
      ...idleLyrics, phase: "done", job_id: "L1", total: 10, processed: 10,
      found: 5, skipped: 2,
    };
    reorganizeData = {
      ...idleReorganize, phase: "running", job_id: "r1",
      processed: 4, total: 11, scope_label: "library",
    };
    const { result } = renderActivity();
    const states = result.current.activity.rows.map((r) => r.state);
    expect(states).toEqual(["done", "running"]);
    expect(result.current.activity.rows[0]?.countsText).toBe(
      "5 found · 2 skipped",
    );
    expect(result.current.activity.runningCount).toBe(1);
  });

  it("drops idle and stopped phases entirely", () => {
    lyricsData = { ...idleLyrics, phase: "stopped", job_id: "L1" };
    artistArtData = { ...idleArtistArt, phase: "stopped", job_id: "a1" };
    const { result } = renderActivity();
    expect(result.current.activity.rows).toEqual([]);
  });

  it("carries the error on failed rows and removes them on dismiss, persisting to sessionStorage", () => {
    reorganizeData = {
      ...idleReorganize, phase: "failed", job_id: "r1", error: "library locked",
    };
    const { result } = renderActivity();
    expect(result.current.activity.rows).toEqual([
      {
        id: "reorganize:r1",
        kind: "reorganize",
        label: "Reorganize",
        scope: "library",
        state: "failed",
        countsText: "library locked",
        href: "/settings/beets",
      },
    ]);

    act(() => result.current.dismissals.dismiss("reorganize:r1"));

    expect(result.current.activity.rows).toEqual([]);
    expect(
      JSON.parse(sessionStorage.getItem("md.activity.dismissed") ?? "[]"),
    ).toEqual(["reorganize:r1"]);
  });

  it("excludes failed rows whose id was dismissed in an earlier mount (sessionStorage round-trip)", () => {
    sessionStorage.setItem(
      "md.activity.dismissed",
      JSON.stringify(["artist-art:a1"]),
    );
    artistArtData = {
      ...idleArtistArt, phase: "failed", job_id: "a1", error: "disk full",
    };
    const { result } = renderActivity();
    expect(result.current.activity.rows).toEqual([]);
    expect(result.current.dismissals.dismissed.has("artist-art:a1")).toBe(true);
  });

  it("never filters running rows by dismissal — only failed ones", () => {
    sessionStorage.setItem(
      "md.activity.dismissed",
      JSON.stringify(["artist-art:a1"]),
    );
    artistArtData = {
      ...idleArtistArt, phase: "running", job_id: "a1",
      processed: 1, total: 4,
    };
    const { result } = renderActivity();
    expect(result.current.activity.rows).toHaveLength(1);
    expect(result.current.activity.runningCount).toBe(1);
  });
});
