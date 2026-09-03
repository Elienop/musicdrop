import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, test, vi } from "vitest";

import {
  markAuthenticated,
  subscribeAuthStore,
  UnauthenticatedError,
} from "@/api/authStore";
import { client } from "@/api/client";
import { apiFetch } from "@/api/lib";
import { createAppQueryClient } from "@/api/queryClient";
import { server } from "@/test/msw-server";

// `/api/health` stands in for "any gated route": what the middleware branches
// on is the schema path NOT being under /api/auth/, and every other endpoint
// in the app is in exactly that position.
const GATED_URL = `${window.location.origin}/api/health`;
const LOGIN_URL = `${window.location.origin}/api/auth/login`;
const STATUS_URL = `${window.location.origin}/api/auth/status`;
const LOGOUT_URL = `${window.location.origin}/api/auth/logout`;
const SETUP_URL = `${window.location.origin}/api/auth/setup`;
const CHANGE_URL = `${window.location.origin}/api/auth/password`;
const RAW_URL = `${window.location.origin}/api/albums/7/cover`;

beforeEach(() => {
  markAuthenticated();
});

/** Count store transitions across a block of work. */
function watchStore() {
  const notified = vi.fn();
  const unsubscribe = subscribeAuthStore(notified);
  return { notified, unsubscribe };
}

describe("client middleware — the session gate's 401", () => {
  test("flips the store and rejects with UnauthenticatedError", async () => {
    server.use(
      http.get(GATED_URL, () =>
        HttpResponse.json({ detail: "authentication required" }, { status: 401 }),
      ),
    );
    const { notified, unsubscribe } = watchStore();

    await expect(client.GET("/api/health")).rejects.toBeInstanceOf(
      UnauthenticatedError,
    );

    expect(notified).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  test("six parallel 401s flip the store exactly once", async () => {
    // What a cold page load with an expired cookie actually looks like: the
    // shell's queries all leave together and all come back refused.
    server.use(
      http.get(GATED_URL, () =>
        HttpResponse.json({ detail: "authentication required" }, { status: 401 }),
      ),
    );
    const { notified, unsubscribe } = watchStore();

    const results = await Promise.allSettled(
      Array.from({ length: 6 }, () => client.GET("/api/health")),
    );

    expect(results.every((r) => r.status === "rejected")).toBe(true);
    expect(notified).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  test("a NON-401 failure leaves the store alone", async () => {
    server.use(
      http.get(GATED_URL, () => new HttpResponse(null, { status: 500 })),
    );
    const { notified, unsubscribe } = watchStore();

    const { response } = await client.GET("/api/health");

    // A 500 is an ordinary failure: it comes back through the result union
    // rather than as a rejection, and nobody gets signed out over it.
    expect(response.status).toBe(500);
    expect(notified).not.toHaveBeenCalled();
    unsubscribe();
  });

  test("a successful response leaves the store alone", async () => {
    server.use(http.get(GATED_URL, () => HttpResponse.json({ status: "ok" })));
    const { notified, unsubscribe } = watchStore();

    const { data } = await client.GET("/api/health");

    expect(data).toEqual({ status: "ok" });
    expect(notified).not.toHaveBeenCalled();
    unsubscribe();
  });
});

describe("client middleware — the exemption is the gate's, exactly", () => {
  test("logout is GATED, so its 401 flips the store like any other route", async () => {
    // The server exempts four exact paths and /api/auth/logout is not one of
    // them (backend app/auth/gate.py::EXEMPT_PATHS, pinned there by
    // test_logout_is_itself_gated). A client exemption written as the prefix
    // "/api/auth/" covered it anyway: the middleware returned without flipping
    // the store, useLogout threw, and the user was stuck in a shell whose
    // session was already dead — every retry reproducing it.
    server.use(
      http.post(LOGOUT_URL, () =>
        HttpResponse.json({ detail: "authentication required" }, { status: 401 }),
      ),
    );
    const { notified, unsubscribe } = watchStore();

    await expect(client.POST("/api/auth/logout")).rejects.toBeInstanceOf(
      UnauthenticatedError,
    );

    expect(notified).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  test("a wrong password does NOT sign the user out", async () => {
    // Without the exemption, submitting a wrong password would flip the store,
    // and the guard would bounce the user off the very page they are trying to
    // sign in on — while the form never gets to show them why it failed.
    server.use(
      http.post(LOGIN_URL, () =>
        HttpResponse.json({ detail: "Incorrect password." }, { status: 401 }),
      ),
    );
    const { notified, unsubscribe } = watchStore();

    const { error, response } = await client.POST("/api/auth/login", {
      body: { password: "wrong" },
    });

    expect(response.status).toBe(401);
    expect(error).toEqual({ detail: "Incorrect password." });
    expect(notified).not.toHaveBeenCalled();
    unsubscribe();
  });

  test("first-run setup is exempt, so it cannot sign out the page creating the password", async () => {
    // The third member of the set, and the one that would be pure decoration if
    // it were not distinguishable: /api/auth/setup is gate-exempt server-side
    // (a server with no password has no credential to hold a session with), so
    // a 401 from it can only be a contract break — and acting on one would flip
    // the store while the operator is halfway through setting their password.
    server.use(
      http.post(SETUP_URL, () =>
        HttpResponse.json({ detail: "authentication required" }, { status: 401 }),
      ),
    );
    const { notified, unsubscribe } = watchStore();

    const { response } = await client.POST("/api/auth/setup", {
      body: { password: "hunter2" },
    });

    expect(response.status).toBe(401);
    expect(notified).not.toHaveBeenCalled();
    unsubscribe();
  });

  test("changing the password is GATED, so its 401 flips the store", async () => {
    // The other direction, and the reason /api/auth/password must stay OUT of
    // the set: it is behind the gate, so its 401 means the session really is
    // gone. Its "wrong current password" answer is a 403 precisely so that a
    // typo can never arrive here looking like an expiry.
    server.use(
      http.post(CHANGE_URL, () =>
        HttpResponse.json({ detail: "authentication required" }, { status: 401 }),
      ),
    );
    const { notified, unsubscribe } = watchStore();

    await expect(
      client.POST("/api/auth/password", {
        body: { current_password: "a", new_password: "b" },
      }),
    ).rejects.toBeInstanceOf(UnauthenticatedError);

    expect(notified).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  test("the status probe is exempt too, so it cannot bounce its own page", async () => {
    // The second half of the exact set. /api/auth/status is gate-exempt
    // server-side and answers 200 for a cookie-less caller, so a 401 here is a
    // contract break rather than a session fact — and treating it as one would
    // have the login page's own admission probe sign the visitor out.
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json({ detail: "authentication required" }, { status: 401 }),
      ),
    );
    const { notified, unsubscribe } = watchStore();

    const { response } = await client.GET("/api/auth/status");

    expect(response.status).toBe(401);
    expect(notified).not.toHaveBeenCalled();
    unsubscribe();
  });
});

describe("apiFetch — the raw-body endpoints", () => {
  test("a 401 flips the store and rejects with UnauthenticatedError", async () => {
    // These hooks never touch openapi-fetch, so they need their own copy of
    // the check or a dead session reads as "Cover install failed".
    server.use(
      http.post(RAW_URL, () =>
        HttpResponse.json({ detail: "authentication required" }, { status: 401 }),
      ),
    );
    const { notified, unsubscribe } = watchStore();

    await expect(
      apiFetch("/api/albums/7/cover", { method: "POST" }),
    ).rejects.toBeInstanceOf(UnauthenticatedError);

    expect(notified).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  test("hands back other statuses untouched, so 404/409 branches still run", async () => {
    server.use(
      http.post(RAW_URL, () => new HttpResponse(null, { status: 409 })),
    );
    const { notified, unsubscribe } = watchStore();

    const res = await apiFetch("/api/albums/7/cover", { method: "POST" });

    expect(res.status).toBe(409);
    expect(notified).not.toHaveBeenCalled();
    unsubscribe();
  });
});

describe("the app query client's retry rule", () => {
  test("does not retry a 401 — one request, then the error", async () => {
    let hits = 0;
    server.use(
      http.get(GATED_URL, () => {
        hits += 1;
        return HttpResponse.json(
          { detail: "authentication required" },
          { status: 401 },
        );
      }),
    );

    const queryClient = createAppQueryClient();
    await expect(
      queryClient.fetchQuery({
        queryKey: ["gated"],
        queryFn: () => client.GET("/api/health"),
      }),
    ).rejects.toBeInstanceOf(UnauthenticatedError);

    expect(hits).toBe(1);
  });

  test("still retries an ordinary failure once — two requests", async () => {
    // The positive control: without it, a predicate that refuses EVERY retry
    // would pass the test above while quietly dropping the app's retry budget.
    let hits = 0;
    server.use(
      http.get(GATED_URL, () => {
        hits += 1;
        return new HttpResponse(null, { status: 500 });
      }),
    );

    const queryClient = createAppQueryClient();
    await expect(
      queryClient.fetchQuery({
        queryKey: ["flaky"],
        retryDelay: 0,
        queryFn: async () => {
          const { data, response } = await client.GET("/api/health");
          if (!response.ok) throw new Error("Health check failed");
          return data;
        },
      }),
    ).rejects.toThrow("Health check failed");

    expect(hits).toBe(2);
  });
});
