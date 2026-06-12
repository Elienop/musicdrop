import { describe, expect, test } from "vitest";

import { detailMessage } from "@/api/lib";

describe("detailMessage", () => {
  test("reads our guards' string detail", () => {
    expect(detailMessage({ detail: "row is queued; decisions need one of [...]" })).toBe(
      "row is queued; decisions need one of [...]",
    );
  });

  test("reads FastAPI's HTTPValidationError array shape (first msg)", () => {
    // FastAPI auto-declares 422 bodies as {detail: [{loc, msg, type}, ...]} —
    // our guard 422s return {detail: string}. Both must surface something human.
    expect(
      detailMessage({ detail: [{ loc: ["body", "path"], msg: "Field required", type: "missing" }] }),
    ).toBe("Field required");
  });

  test("returns null for anything else", () => {
    expect(detailMessage(undefined)).toBeNull();
    expect(detailMessage(null)).toBeNull();
    expect(detailMessage("boom")).toBeNull();
    expect(detailMessage({ detail: 42 })).toBeNull();
    expect(detailMessage({ detail: [] })).toBeNull();
    expect(detailMessage({ detail: [{ nope: true }] })).toBeNull();
  });
});
