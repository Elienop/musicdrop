import { describe, expect, test } from "vitest";

import { formatBytes, formatTotalDuration } from "@/lib/format";

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
