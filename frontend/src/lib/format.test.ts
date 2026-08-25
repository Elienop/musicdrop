import { describe, expect, test } from "vitest";

import {
  formatBytes,
  formatDuration,
  formatTimestamp,
  formatTotalDuration,
  plural,
} from "@/lib/format";

describe("formatTotalDuration", () => {
  test("days for >= 1 day", () => {
    expect(formatTotalDuration(544_320)).toBe("6.3 days"); // 6.3 * 86400
  });
  test("hours + minutes for < 1 day", () => {
    expect(formatTotalDuration(67_320)).toBe("18h 42m"); // 18*3600 + 42*60
  });
  test("minutes for < 1 hour", () => {
    expect(formatTotalDuration(312)).toBe("5m"); // 5m 12s -> 5m
  });
  test("zero", () => {
    expect(formatTotalDuration(0)).toBe("0m");
  });
});

describe("formatBytes", () => {
  test("GB with one decimal", () => {
    expect(formatBytes(74_200_000_000)).toBe("74.2 GB");
  });
  test("MB", () => {
    expect(formatBytes(5_500_000)).toBe("5.5 MB");
  });
  test("bytes (no decimal)", () => {
    expect(formatBytes(512)).toBe("512 B");
  });
  test("zero", () => {
    expect(formatBytes(0)).toBe("0 B");
  });
});

describe("formatDuration", () => {
  test("m:ss with zero-padded seconds", () => {
    expect(formatDuration(284)).toBe("4:44");
  });
  test("pads sub-10-second durations", () => {
    expect(formatDuration(5)).toBe("0:05");
  });
  test("floors fractional seconds", () => {
    expect(formatDuration(59.9)).toBe("0:59");
  });
  test("minutes never roll into hours (long tracks)", () => {
    expect(formatDuration(3725)).toBe("62:05");
  });
  test("zero", () => {
    expect(formatDuration(0)).toBe("0:00");
  });
  test("en-dash for a missing duration", () => {
    expect(formatDuration(null)).toBe("-");
  });
});

describe("plural", () => {
  test("singular for exactly 1", () => {
    expect(plural(1, "track")).toBe("track");
  });
  test("default plural for > 1", () => {
    expect(plural(3, "track")).toBe("tracks");
  });
  test("plural at 0", () => {
    expect(plural(0, "group")).toBe("groups");
  });
  test("irregular via the third argument", () => {
    expect(plural(1, "folder is", "folders are")).toBe("folder is");
    expect(plural(2, "folder is", "folders are")).toBe("folders are");
  });
});

describe("formatTimestamp", () => {
  // The locale and the time zone belong to the reader (CI runs UTC/en-US, a
  // laptop does not), so these assert the SHAPE of the output rather than one
  // runner's exact wording.
  test("renders a wire timestamp as a date and time, never the raw ISO", () => {
    const iso = "2026-08-02T13:53:00Z";
    const out = formatTimestamp(iso);
    expect(out).not.toContain(iso);
    expect(out).toContain("2026"); // the year survives every locale
    expect(out).toMatch(/\d{1,2}[:.]\d{2}/); // ...and so does an hour:minute
  });

  test("two instants an hour apart never render the same", () => {
    // Guards the time half: a date-only format collapses these into one string,
    // and a result from this morning would read exactly like one from tonight.
    expect(formatTimestamp("2026-08-02T13:53:00Z")).not.toBe(
      formatTimestamp("2026-08-02T14:53:00Z"),
    );
  });
});
