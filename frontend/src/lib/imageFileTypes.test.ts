import { describe, expect, it } from "vitest";

import { isConfidentlyUnsupportedImageType } from "@/lib/imageFileTypes";

describe("isConfidentlyUnsupportedImageType", () => {
  // The four `sniff_image_mime` recognises in backend/app/artwork/images.py.
  it.each(["image/png", "image/jpeg", "image/gif", "image/webp"])(
    "does not refuse %s, which the server sniffs and accepts",
    (type) => {
      expect(isConfidentlyUnsupportedImageType(type)).toBe(false);
    },
  );

  // A declared type naming a real format the sniffer has no branch for. The
  // upload was always going to 415, so refusing it client-side is a true
  // refusal and must survive the leniency change.
  it.each(["image/bmp", "image/tiff", "image/svg+xml", "application/pdf", "text/plain", "audio/mpeg"])(
    "refuses %s, which the server would reject anyway",
    (type) => {
      expect(isConfidentlyUnsupportedImageType(type)).toBe(true);
    },
  );

  // The bug this predicate exists to prevent. Both values mean "the browser
  // does not know", and the browser only ever knew the extension in the first
  // place — the bytes may well be a PNG the server will happily sniff.
  it("does not refuse a blank type, which is what an extensionless file reports", () => {
    expect(isConfidentlyUnsupportedImageType("")).toBe(false);
  });

  it("does not refuse application/octet-stream, which asserts nothing about the bytes", () => {
    expect(isConfidentlyUnsupportedImageType("application/octet-stream")).toBe(false);
  });

  // Negative control against a mutant that simply returns false — leniency is
  // scoped to the two non-claims above, not extended to every `application/*`.
  it("still refuses a different application/* type", () => {
    expect(isConfidentlyUnsupportedImageType("application/zip")).toBe(true);
  });
});
