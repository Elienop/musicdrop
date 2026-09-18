import { describe, expect, test } from "vitest";

import { detailMessage, errorDetail, unwrap } from "@/api/lib";

/** A minimal Response-like whose `json()` yields `body`. */
function jsonRes(body: unknown): Response {
  return { json: async () => body } as unknown as Response;
}

/** A Response-like carrying only the `ok` flag `unwrap` reads. */
function okRes(ok: boolean): Response {
  return { ok } as unknown as Response;
}

/** A Response-like whose body isn't JSON (mirrors a non-JSON error page). */
function nonJsonRes(): Response {
  return {
    json: async () => {
      throw new SyntaxError("Unexpected token < in JSON");
    },
  } as unknown as Response;
}

describe("detailMessage", () => {
  test("reads our guards' string detail", () => {
    expect(detailMessage({ detail: "row is queued; decisions need one of [...]" })).toBe(
      "row is queued; decisions need one of [...]",
    );
  });

  test("reads a structured guard object detail ({message, recovery})", () => {
    // Cover install / delete / config-apply guards raise
    // {detail: {message, recovery}} on 500 — the message carries the real cause.
    expect(
      detailMessage({
        detail: {
          message: "Cover install failed: [Errno 2] No such file or directory",
          recovery: "Reload and retry.",
        },
      }),
    ).toBe("Cover install failed: [Errno 2] No such file or directory");
  });

  test("reads FastAPI's HTTPValidationError array shape (first msg)", () => {
    // FastAPI auto-declares 422 bodies as {detail: [{loc, msg, type}, ...]} —
    // our guard 422s return {detail: string}. Both must surface something human.
    expect(
      detailMessage({ detail: [{ loc: ["body", "path"], msg: "Field required", type: "missing" }] }),
    ).toBe("Field required");
  });

  // A blank detail is no detail: `""` passed every caller's `??` and rendered
  // an empty red alert — and, on the import panel, an empty aria-describedby
  // target with it. Whitespace counts as blank for the same reason.
  test("returns null for a blank detail, in every shape", () => {
    expect(detailMessage({ detail: "" })).toBeNull();
    expect(detailMessage({ detail: "   " })).toBeNull();
    expect(detailMessage({ detail: [{ msg: "" }] })).toBeNull();
    expect(detailMessage({ detail: { message: "  " } })).toBeNull();
  });

  test("returns null for anything else", () => {
    expect(detailMessage(undefined)).toBeNull();
    expect(detailMessage(null)).toBeNull();
    expect(detailMessage("boom")).toBeNull();
    expect(detailMessage({ detail: 42 })).toBeNull();
    expect(detailMessage({ detail: [] })).toBeNull();
    expect(detailMessage({ detail: [{ nope: true }] })).toBeNull();
    expect(detailMessage({ detail: { recovery: "no message here" } })).toBeNull();
    expect(detailMessage({ detail: { message: 7 } })).toBeNull();
  });
});

describe("errorDetail", () => {
  test("unwraps a structured guard detail ({message, recovery}) to its message", async () => {
    const res = jsonRes({
      detail: {
        message: "Cover install failed: [Errno 13] Permission denied",
        recovery: "Reload and retry.",
      },
    });
    expect(await errorDetail(res, "Cover install failed")).toBe(
      "Cover install failed: [Errno 13] Permission denied",
    );
  });

  test("still reads a plain string detail", async () => {
    expect(await errorDetail(jsonRes({ detail: "Image is too large (max 10 MB)." }), "x")).toBe(
      "Image is too large (max 10 MB).",
    );
  });

  test("falls back on a non-JSON body", async () => {
    expect(await errorDetail(nonJsonRes(), "generic")).toBe("generic");
  });

  test("falls back when the detail has no usable message", async () => {
    expect(await errorDetail(jsonRes({ detail: { recovery: "x" } }), "generic")).toBe("generic");
    expect(await errorDetail(jsonRes({ nope: true }), "generic")).toBe("generic");
  });
});

describe("unwrap", () => {
  test("returns the data on a 2xx result", () => {
    const value = { id: 7 };
    expect(unwrap({ data: value, response: okRes(true) }, "boom")).toBe(value);
  });

  test("throws the message on a transport error", () => {
    expect(() =>
      unwrap({ error: { detail: "nope" }, response: okRes(true) }, "boom"),
    ).toThrow("boom");
  });

  test("throws on a non-2xx even when a body IS present", () => {
    // The correctness fix: a non-2xx that still parsed a body must throw, not
    // return the body as if it were success data.
    expect(() =>
      unwrap({ data: { id: 1 }, response: okRes(false) }, "boom"),
    ).toThrow("boom");
  });

  test("throws on a non-2xx with no body (bodyless 5xx)", () => {
    expect(() => unwrap({ response: okRes(false) }, "boom")).toThrow("boom");
  });

  test("throws when data is missing on a 2xx", () => {
    expect(() => unwrap({ data: undefined, response: okRes(true) }, "boom")).toThrow(
      "boom",
    );
    expect(() => unwrap({ data: null, response: okRes(true) }, "boom")).toThrow("boom");
  });
});
