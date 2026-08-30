import { beforeEach, describe, expect, test, vi } from "vitest";

import {
  markAuthenticated,
  markUnauthenticated,
  subscribeAuthStore,
  UnauthenticatedError,
} from "@/api/authStore";

// Module state outlives a test, so every case starts from "signed in".
beforeEach(() => {
  markAuthenticated();
});

describe("auth store", () => {
  test("notifies once per TRANSITION, not once per refusal", () => {
    // The trigger this exists for: one page mounts half a dozen queries that
    // all meet the same expired cookie and all call markUnauthenticated within
    // the same tick.
    const notified = vi.fn();
    const unsubscribe = subscribeAuthStore(notified);

    markUnauthenticated();
    markUnauthenticated();
    markUnauthenticated();
    markUnauthenticated();
    markUnauthenticated();
    markUnauthenticated();

    expect(notified).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  test("signing back in re-arms it, so a later expiry notifies again", () => {
    const notified = vi.fn();
    const unsubscribe = subscribeAuthStore(notified);

    markUnauthenticated();
    markAuthenticated();
    markUnauthenticated();

    expect(notified).toHaveBeenCalledTimes(3);
    unsubscribe();
  });

  test("marking authenticated while already signed in notifies nobody", () => {
    const notified = vi.fn();
    const unsubscribe = subscribeAuthStore(notified);

    markAuthenticated();

    expect(notified).not.toHaveBeenCalled();
    unsubscribe();
  });

  test("unsubscribing stops the notifications", () => {
    const notified = vi.fn();
    subscribeAuthStore(notified)();

    markUnauthenticated();

    expect(notified).not.toHaveBeenCalled();
  });
});

describe("UnauthenticatedError", () => {
  test("is a real Error subclass, so `instanceof` can gate a retry", () => {
    const error = new UnauthenticatedError();

    expect(error).toBeInstanceOf(Error);
    expect(error).toBeInstanceOf(UnauthenticatedError);
    expect(error.name).toBe("UnauthenticatedError");
    // A plain Error must NOT satisfy the check the retry predicate makes,
    // or every failure would stop retrying.
    expect(new Error("boom")).not.toBeInstanceOf(UnauthenticatedError);
  });
});
