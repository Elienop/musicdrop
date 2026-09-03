import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import * as assetVersion from "@/api/assetVersion";
import type { PasswordSource } from "@/api/auth";
import { AUTH_STATUS_KEY } from "@/api/auth";
import { markAuthenticated, markUnauthenticated } from "@/api/authStore";
import { LIBRARY_CONTENT_KEY_COUNT } from "@/api/useEventStream";
import { LoginPage } from "@/pages/LoginPage";
import { server } from "@/test/msw-server";

/** Captured before any test stubs it — see the afterEach below. */
const REAL_NAVIGATOR = globalThis.navigator;

const STATUS_URL = `${window.location.origin}/api/auth/status`;
const LOGIN_URL = `${window.location.origin}/api/auth/login`;
const SETUP_URL = `${window.location.origin}/api/auth/setup`;

/**
 * What GET /api/auth/status reports to a signed-out browser.
 *
 * `source` defaults to the value that matches `passwordSet`, so the calls that
 * only care whether signing in is possible read as before: a usable password is
 * one stored in a file, and an unusable one with no source is a first run. The
 * two combinations that are neither — false with "env" and false with "file" —
 * are the unreadable-hash branches and are always passed explicitly.
 */
function statusHandler(
  passwordSet: boolean,
  authenticated = false,
  source: PasswordSource = passwordSet ? "file" : "none",
) {
  return http.get(STATUS_URL, () =>
    HttpResponse.json({
      authenticated,
      password_set: passwordSet,
      password_source: source,
    }),
  );
}

/** Exposes the live router location so tests can assert where sign-in landed. */
function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{`${loc.pathname}${loc.search}`}</div>;
}

/** Own render helper rather than `renderWithProviders`, because these tests
 * need the QueryClient itself — the login success path is defined by what it
 * does to the cache. */
function renderLogin(state?: unknown) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const bump = vi
    .spyOn(assetVersion, "bumpAssetVersion")
    .mockImplementation(() => {});
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[{ pathname: "/login", state }]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="*" element={<div>shell stand-in</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { view, queryClient, invalidate, bump };
}

beforeEach(() => {
  markAuthenticated();
  vi.restoreAllMocks();
});

afterEach(() => {
  // Restore ONLY navigator (the clipboard test stubs it). `vi.unstubAllGlobals()`
  // would also drop test/setup.ts's scrollTo/matchMedia/EventSource stubs for
  // the rest of this file — and unstubbing at the END of the one test that
  // stubs left a stubbed navigator behind for every test after it whenever
  // that test failed part-way.
  vi.stubGlobal("navigator", REAL_NAVIGATOR);
});

describe("LoginPage — the form", () => {
  test("offers a password field wired for a password manager", async () => {
    server.use(statusHandler(true));
    renderLogin();

    const field = await screen.findByLabelText("Password");
    // `current-password` (not `new-password`): this is the sign-in field, so a
    // manager should offer the saved entry rather than propose a fresh one.
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    // The brand is the page heading; the wordmark itself is decorative.
    expect(
      screen.getByRole("heading", { level: 1, name: "MusicDrop" }),
    ).toBeInTheDocument();
    // And the card's own title is a real heading under it — CardTitle renders
    // a div, so without this "Sign in" was not in the document outline.
    expect(
      screen.getByRole("heading", { level: 2, name: "Sign in" }),
    ).toBeInTheDocument();
  });

  test("carries the username field password managers look for", async () => {
    // Chrome logs "Password forms should have (optionally hidden) username
    // fields for accessibility" for a form with password fields alone, and
    // managers file entries they then struggle to offer back. It is off
    // screen, out of the tab order and out of the accessibility tree: this
    // server has one password and no accounts, so it is not a second thing for
    // anyone to fill in.
    server.use(statusHandler(true));
    renderLogin();

    const password = await screen.findByLabelText("Password");
    const form = password.closest("form");
    const username = form?.querySelector('input[autocomplete="username"]');
    expect(username).toBeInstanceOf(HTMLInputElement);
    expect(username).toHaveAttribute("tabindex", "-1");
    expect(username).toHaveAttribute("aria-hidden", "true");
    expect(username).toHaveAttribute("readonly");
    // Present, not merely declared: `display: none` is the shape managers skip.
    expect(username).not.toHaveClass("hidden");
  });

  test("shows nothing decidable until the status probe answers", async () => {
    server.use(statusHandler(true));
    renderLogin();

    // A form that flipped to "no password configured" a beat later would read
    // as a fault, so neither branch renders while the probe is in flight.
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(screen.queryByText(/no password is configured/i)).not.toBeInTheDocument();

    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
  });
});

describe("LoginPage — the server's own rejections, verbatim", () => {
  // The server's own sentences, copied from the `_*_DETAIL` constants in
  // backend/app/api/auth.py — these are the strings the UI must not
  // paraphrase, so the fixture has to BE them. (A fixture that has drifted
  // proves nothing about verbatim rendering: the row that used to sit here,
  // "The configured password hash is not readable.", was a sentence the
  // backend does not send, under a comment citing a file that does not exist.)
  test.each([
    [401, "Incorrect password."],
    [401, "No password is configured on this server."],
    [
      401,
      "The password hash in MUSICDROP_PASSWORD_HASH is not readable. In docker-compose, every $ in the hash must be doubled to $$. Or unset it and restart MusicDrop, and the password stored on this server, if there is one, applies again.",
    ],
    [
      401,
      "The stored password hash on this server is not readable. Delete the password-hash file in the beets directory and restart MusicDrop to set a new password.",
    ],
    [429, "Another sign-in is already in progress. Try again in a moment."],
    [503, "The session signing secret is unavailable."],
  ])("renders the %i detail as written", async (status, detail) => {
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () => HttpResponse.json({ detail }, { status })),
    );
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    // Each detail names its own cause; paraphrasing here would put a second,
    // drifting copy of the backend's vocabulary in the UI.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(detail);
  });

  test("a 429 leaves the form usable, so the retry it asks for is possible", async () => {
    let attempts = 0;
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () => {
        attempts += 1;
        return attempts === 1
          ? HttpResponse.json(
              {
                detail:
                  "Another sign-in is already in progress. Try again in a moment.",
              },
              { status: 429 },
            )
          : HttpResponse.json({ authenticated: true, password_set: true });
      }),
    );
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    const submit = screen.getByRole("button", { name: "Sign in" });
    await userEvent.click(submit);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Another sign-in is already in progress.",
    );
    expect(submit).toBeEnabled();
    expect(screen.getByLabelText("Password")).toBeEnabled();

    // The password the user typed is still there — a second click is all the
    // retry costs.
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(attempts).toBe(2));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/"),
    );
  });

  test("a failed sign-in does not bounce the user off this page", async () => {
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () =>
        HttpResponse.json({ detail: "Incorrect password." }, { status: 401 }),
      ),
    );
    renderLogin({ from: { pathname: "/artists", search: "" } });

    await userEvent.type(await screen.findByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByRole("alert");
    // Still on /login, with the destination still remembered for the next try.
    expect(screen.getByTestId("location")).toHaveTextContent("/login");
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });

  test("an unreachable backend is explained, not reported in browser jargon", async () => {
    // A transport failure makes client.POST REJECT rather than resolve, so it
    // never reaches the detail handling — and the browser's own words ("Failed
    // to fetch" / "NetworkError when attempting to fetch resource") landed in
    // the alert, on the one screen built to explain sign-in problems well.
    server.use(statusHandler(true), http.post(LOGIN_URL, () => HttpResponse.error()));
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Can’t reach the server. Check that MusicDrop is running, then try again.",
    );
    expect(alert).not.toHaveTextContent(/fetch/i);
  });

  test("marks the field invalid and points the rejection at it", async () => {
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () =>
        HttpResponse.json({ detail: "Incorrect password." }, { status: 401 }),
      ),
    );
    renderLogin();

    const field = await screen.findByLabelText("Password");
    // The primitive styles `aria-invalid` (destructive border + ring); before
    // a rejection there is nothing to style.
    expect(field).toHaveAttribute("aria-invalid", "false");
    expect(field).not.toHaveAttribute("aria-describedby");

    await userEvent.type(field, "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    expect(field).toHaveAttribute("aria-invalid", "true");
    // The message is ABOUT the field, not merely near it.
    expect(field).toHaveAttribute("aria-describedby", alert.id);
    expect(alert.id).toBeTruthy();
  });

  test("puts focus back in the field with the rejected password selected", async () => {
    // Clicking Sign in disables the button, which drops focus to <body>: the
    // alert then announces to a user whose focus is nowhere, with the rejected
    // password still typed and needing a select-all before the retry.
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () =>
        HttpResponse.json({ detail: "Incorrect password." }, { status: 401 }),
      ),
    );
    renderLogin();

    const field = await screen.findByLabelText<HTMLInputElement>("Password");
    await userEvent.type(field, "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByRole("alert");
    expect(field).toHaveFocus();
    expect(field.selectionStart).toBe(0);
    expect(field.selectionEnd).toBe("wrong".length);
  });

  test.each([
    [429, "Another sign-in is already in progress. Try again in a moment."],
    [503, "The session signing secret is unavailable."],
  ])("says the %i in the alert without marking the field", async (status, detail) => {
    // `aria-invalid` and `aria-describedby` claim the sentence is ABOUT the
    // value in this field. A derive already running, and a server with no
    // signing secret, are about the request and about the server — marking the
    // field for them told a screen-reader user their password was the problem.
    // The rule the setup form and the Account panel already follow.
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () => HttpResponse.json({ detail }, { status })),
    );
    renderLogin();

    const field = await screen.findByLabelText<HTMLInputElement>("Password");
    await userEvent.type(field, "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(field).toHaveAttribute("aria-invalid", "false");
    expect(field).not.toHaveAttribute("aria-describedby");
    // Focus still comes back — the disabled button dropped it to <body>, and
    // the retry both of these ask for starts here. NOT selected, though:
    // neither sentence says the typed value is the problem.
    expect(field).toHaveFocus();
    expect(field.selectionEnd).toBe(field.selectionStart);
  });

  test("the sentence for a server that never answered marks no field either", async () => {
    // Written on this side rather than by the server (see useLogin), and about
    // reachability — so it is about neither the password nor anything else on
    // screen, and points at no field.
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () => HttpResponse.error()),
    );
    renderLogin();

    const field = await screen.findByLabelText("Password");
    await userEvent.type(field, "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Can’t reach the server.",
    );
    expect(field).toHaveAttribute("aria-invalid", "false");
    expect(field).not.toHaveAttribute("aria-describedby");
  });

  test("retyping after a refused password clears the marking and the sentence", async () => {
    // Typing answers the one sentence that is about the value in this field,
    // so it goes on the edit rather than standing over a password the user has
    // already replaced — parity with the setup form's mismatch and the Account
    // panel's wrong current password.
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () =>
        HttpResponse.json({ detail: "Incorrect password." }, { status: 401 }),
      ),
    );
    renderLogin();

    const field = await screen.findByLabelText("Password");
    await userEvent.type(field, "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(field).toHaveAttribute("aria-invalid", "true");

    await userEvent.type(field, "right");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(field).toHaveAttribute("aria-invalid", "false");
    expect(field).not.toHaveAttribute("aria-describedby");
  });

  test("a sentence about the request stands while the field is retyped", async () => {
    // A 429 is not answered by typing — the derive slot is busy whatever is in
    // the field — so it stays until the next submit decides again. Only the
    // sentence about THIS field's value is cleared by editing it.
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () =>
        HttpResponse.json(
          {
            detail:
              "Another sign-in is already in progress. Try again in a moment.",
          },
          { status: 429 },
        ),
      ),
    );
    renderLogin();

    const field = await screen.findByLabelText("Password");
    await userEvent.type(field, "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Another sign-in is already",
    );

    await userEvent.type(field, "3");

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Another sign-in is already",
    );
  });

  test("the last answer's marking does not outlive the next submit", async () => {
    // A refused password marks the field; a 429 on the retry is about the
    // request. Read off `login.isError` the marking survived both, so the
    // second answer arrived with the first one's red border still on the field.
    let posts = 0;
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () => {
        posts += 1;
        return posts === 1
          ? HttpResponse.json({ detail: "Incorrect password." }, { status: 401 })
          : HttpResponse.json(
              {
                detail:
                  "Another sign-in is already in progress. Try again in a moment.",
              },
              { status: 429 },
            );
      }),
    );
    renderLogin();

    const field = await screen.findByLabelText("Password");
    await userEvent.type(field, "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Incorrect password.",
    );
    expect(field).toHaveAttribute("aria-invalid", "true");

    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Another sign-in is already",
      ),
    );
    expect(field).toHaveAttribute("aria-invalid", "false");
    expect(field).not.toHaveAttribute("aria-describedby");
  });

  test("the previous answer is cleared before the next one lands", async () => {
    // While the retry is in flight the last answer is no longer the answer to
    // anything: leaving it up marks the field red under a request that has not
    // been refused yet, and its `role="alert"` is the page's account of a
    // submit two clicks old.
    let posts = 0;
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, async () => {
        posts += 1;
        if (posts === 1) {
          return HttpResponse.json(
            { detail: "Incorrect password." },
            { status: 401 },
          );
        }
        // Never answers: the assertions below are about the moment between the
        // click and the answer.
        await delay("infinite");
        return HttpResponse.json({ authenticated: true, password_set: true });
      }),
    );
    renderLogin();

    const field = await screen.findByLabelText("Password");
    await userEvent.type(field, "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Incorrect password.",
    );
    expect(field).toHaveAttribute("aria-invalid", "true");

    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(
      await screen.findByRole("button", { name: "Signing in…" }),
    ).toBeDisabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(field).toHaveAttribute("aria-invalid", "false");
    expect(field).not.toHaveAttribute("aria-describedby");
  });

  test("a 401 about the SERVER hands over to the branch that names the fix", async () => {
    // The route answers 401 for a wrong password AND for a server where
    // nothing can authenticate — no hash, or one that does not parse
    // (backend/app/api/auth.py::_refusal_detail). This page branches on the
    // status probe at load, so the second kind reaches the form only when the
    // server's state changed while the page was open.
    //
    // The form does not read the sentence to tell them apart: a 401 marks the
    // field, and the re-check behind it swaps the whole card for the branch
    // that names the fix — the same move the setup form makes on a 409.
    // Matching on the server's prose would break the moment it is reworded.
    let statusCalls = 0;
    server.use(
      http.get(STATUS_URL, () => {
        statusCalls += 1;
        return HttpResponse.json({
          authenticated: false,
          password_set: statusCalls === 1,
          password_source: statusCalls === 1 ? "file" : "env",
        });
      }),
      http.post(LOGIN_URL, () =>
        HttpResponse.json(
          {
            detail:
              "The password hash in MUSICDROP_PASSWORD_HASH is not readable. In docker-compose, every $ in the hash must be doubled to $$. Or unset it and restart MusicDrop, and the password stored on this server, if there is one, applies again.",
          },
          { status: 401 },
        ),
      ),
    );
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    // The form is gone, and what replaced it is the env branch's banner — the
    // one screen that says what to do about an unreadable variable.
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveAttribute(
        "data-slot",
        "status-banner",
      ),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "MUSICDROP_PASSWORD_HASH",
    );
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(statusCalls).toBe(2);
  });

  test("an empty submit is refused by the field, not by a scrypt round trip", async () => {
    // Verifying a password is a ~128 MiB scrypt derive; `required` turns an
    // empty submit into instant native feedback instead of a request.
    let posted = 0;
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () => {
        posted += 1;
        return HttpResponse.json({ authenticated: true, password_set: true });
      }),
    );
    renderLogin();

    expect(await screen.findByLabelText("Password")).toBeRequired();
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(posted).toBe(0);
    expect(screen.getByTestId("location")).toHaveTextContent("/login");
  });
});

describe("LoginPage — signing in", () => {
  test("seeds the cache, refreshes the library, and lands on the page asked for", async () => {
    const authStatus = { authenticated: true, password_set: true };
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () => HttpResponse.json(authStatus)),
    );
    const { queryClient, invalidate, bump } = renderLogin({
      from: { pathname: "/artists", search: "?letter=R" },
    });

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/artists?letter=R",
      ),
    );

    // The login response IS an AuthStatus, so the guard's query starts warm
    // instead of round-tripping again on the way into the shell.
    expect(queryClient.getQueryData(AUTH_STATUS_KEY)).toEqual(authStatus);
    // One full invalidation round: this browser has been away, and a FRESH
    // EventSource replays nothing (its catch-up only runs on a re-connect).
    const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey);
    expect(keys).toHaveLength(LIBRARY_CONTENT_KEY_COUNT);
    expect(keys).toEqual(
      expect.arrayContaining([["albums"], ["artists"], ["playlists"]]),
    );
    // Every <img> remounts, so art that changed while away is re-requested.
    expect(bump).toHaveBeenCalled();
  });

  test("with no remembered destination, lands on the Overview", async () => {
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () =>
        HttpResponse.json({ authenticated: true, password_set: true }),
      ),
    );
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("shell stand-in")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/");
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });

  // The remembered location is read back as `unknown`: it only has to LOOK
  // like a Location. Under react-router 7 a cross-origin `replace` calls
  // `history.replaceState`, which THROWS a SecurityError — so a destination
  // that leaves the app strands the user immediately after a sign-in that
  // worked, and one dropped `replace:` away it would be a working open
  // redirect instead. Every row here passed the `pathname.startsWith("/")`
  // check this replaced.
  test.each([
    ["an absolute URL", { pathname: "https://example.invalid/steal", search: "" }],
    ["a protocol-relative path", { pathname: "//evil.com", search: "" }],
    ["a backslash-escaped one", { pathname: "/\\evil.com", search: "" }],
    // `search` was concatenated onto `pathname` with no check of its own, so
    // two innocent-looking halves composed into "//evil.com".
    ["a hostile search half", { pathname: "/", search: "/evil.com" }],
    // Dot segments. The guard resolved these on THIS origin (they are paths,
    // so `url.origin` is ours) and then RETURNED the parser's normalised
    // pathname — which for each of these is the literal "//evil.com", the one
    // protocol-relative shape the guard exists to reject. It was not closed
    // under its own output: hand it back what it just handed you and it says
    // null. No reachable input was found (the WHATWG parser strips dot
    // segments before `location.pathname` can hold one, and RequireAuth is the
    // only writer of this state), so these are latent — but a function must
    // not emit a destination it would itself refuse.
    ["a dot segment", { pathname: "/.//evil.com", search: "" }],
    ["a parent segment", { pathname: "/..//evil.com", search: "" }],
    ["a parent segment mid-path", { pathname: "/x/..//evil.com", search: "" }],
  ])("refuses %s as a destination", async (_label, from) => {
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () =>
        HttpResponse.json({ authenticated: true, password_set: true }),
      ),
    );
    renderLogin({ from });

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("shell stand-in")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/");
    expect(screen.getByTestId("location")).not.toHaveTextContent("evil");
    expect(screen.getByTestId("location")).not.toHaveTextContent("example");
  });

  test("a double-slash URL with no attacker in it still lands somewhere sane", async () => {
    // Reachable with nobody hostile involved: the SPA fallback serves
    // index.html for "http://host:3030//albums", so a signed-out visitor to a
    // double-slash URL is bounced here with that as their remembered page —
    // and their successful sign-in used to throw on the way back.
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () =>
        HttpResponse.json({ authenticated: true, password_set: true }),
      ),
    );
    renderLogin({ from: { pathname: "//albums", search: "" } });

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("shell stand-in")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/");
  });

  test("does not leave the typed password sitting in the mutation cache", async () => {
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () =>
        HttpResponse.json({ authenticated: true, password_set: true }),
      ),
    );
    const { queryClient } = renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("shell stand-in");

    // TanStack keeps `state.variables` — here, the plaintext password — for
    // gcTime after the form unmounts; the default is five minutes.
    await waitFor(() =>
      expect(queryClient.getMutationCache().getAll()).toHaveLength(0),
    );
  });

  test("an already-signed-in visitor is sent on rather than shown a dead form", async () => {
    server.use(statusHandler(true, true));
    renderLogin({ from: { pathname: "/browse", search: "" } });

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/browse"),
    );
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });
});

describe("LoginPage — landing here from inside the shell", () => {
  const SIGNED_OUT_NOTE = "You’ve been signed out. Sign in again to continue.";

  test("says why, when the session ended mid-visit", async () => {
    // The store flip plus a remembered page is what a mid-session expiry (or a
    // Sign out click) looks like from here. Without this the user is dropped on
    // the same cold-visit copy with no account of what just happened — and the
    // store's own "Your session has expired" reaches nobody.
    server.use(statusHandler(true));
    renderLogin({ from: { pathname: "/artists", search: "" } });
    await screen.findByLabelText("Password");

    act(() => {
      markUnauthenticated();
    });

    expect(await screen.findByText(SIGNED_OUT_NOTE)).toBeInTheDocument();
  });

  test("stays quiet for a cold visit, which explains nothing about a session", async () => {
    // The absence control. A signed-out browser that was never signed in is
    // bounced by the STATUS probe, which never touches the store — so the note
    // must not fire on the ordinary first visit to a gated URL.
    server.use(statusHandler(true));
    renderLogin({ from: { pathname: "/artists", search: "" } });

    await screen.findByLabelText("Password");
    expect(screen.queryByText(SIGNED_OUT_NOTE)).not.toBeInTheDocument();
  });

  test("takes the tab title back off the page the user was bounced from", async () => {
    // RouteAnnouncer is the app's only writer of document.title and it
    // unmounts with the shell, so the tab kept reading "Artists - MusicDrop"
    // over a sign-in form.
    document.title = "Artists - MusicDrop";
    server.use(statusHandler(true));
    renderLogin();

    await waitFor(() => expect(document.title).toBe("Sign in - MusicDrop"));
  });
});

describe("LoginPage — first run, with no password anywhere", () => {
  test("titles the card for the thing this screen does, and says it once", async () => {
    // The card header is the first thing announced after the brand, and over a
    // form whose only action is "Set password" it used to read "Sign in" — with
    // a second muted paragraph under the description repeating what the
    // description had just said.
    server.use(statusHandler(false, false, "none"));
    renderLogin();

    await screen.findByRole("button", { name: "Set password" });
    expect(
      screen.getByRole("heading", { level: 2, name: "Set a password" }),
    ).toBeInTheDocument();
    // One h2 in the card: the branch REPLACES the sign-in title rather than
    // adding a second heading beside it.
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
    expect(
      screen.getByText(
        "No password is set yet. Choose the one everyone will use to sign in to this server.",
      ),
    ).toBeInTheDocument();
    // The sign-in description is not also on screen — the two would read as one
    // two-sentence muted block with an odd gap in the middle.
    expect(
      screen.queryByText("MusicDrop is protected by a single password."),
    ).not.toBeInTheDocument();
    // And the tab follows the header, rather than announcing a form that is
    // not the one on screen.
    await waitFor(() =>
      expect(document.title).toBe("Set a password - MusicDrop"),
    );
  });

  test("sets the password here instead of explaining a shell command", async () => {
    server.use(statusHandler(false, false, "none"));
    renderLogin();

    // Two fields, because the CLI this replaces prompts twice: a typo in a
    // single masked field locks the operator out of their own server.
    const password = await screen.findByLabelText("Password");
    const confirm = screen.getByLabelText("Confirm password");
    expect(password).toHaveAttribute("type", "password");
    // `new-password` on BOTH: nothing is saved for this server yet, so a
    // manager should offer to generate and store one rather than autofill an
    // entry that cannot exist.
    expect(password).toHaveAttribute("autocomplete", "new-password");
    expect(confirm).toHaveAttribute("autocomplete", "new-password");
    expect(
      screen.getByRole("button", { name: "Set password" }),
    ).toBeInTheDocument();
    // The half of the old notice that is now WRONG for this branch: nothing
    // here needs the env var or the hash command, and naming either would send
    // the operator to a shell they no longer have to open.
    expect(screen.queryByText("MUSICDROP_PASSWORD_HASH")).not.toBeInTheDocument();
    expect(screen.queryByText(/hash_password/)).not.toBeInTheDocument();
    // No re-check button under the primary action either: the case it covers
    // here (a password set from elsewhere while this page was open) is what the
    // 409 on submit answers, so on this branch it was a second full-width
    // button competing with the one the operator came to press.
    expect(
      screen.queryByRole("button", { name: "Check again" }),
    ).not.toBeInTheDocument();
  });

  test("the setup form carries the username field password managers look for", async () => {
    // Pinned on this form of its own: the sign-in form's copy of this test
    // renders the OTHER branch, so deleting the field here left the suite
    // green. Off screen, not a tab stop, not announced.
    server.use(statusHandler(false, false, "none"));
    renderLogin();

    const password = await screen.findByLabelText("Password");
    const username = password
      .closest("form")
      ?.querySelector('input[autocomplete="username"]');
    expect(username).toBeInstanceOf(HTMLInputElement);
    expect(username).toHaveAttribute("tabindex", "-1");
    expect(username).toHaveAttribute("aria-hidden", "true");
    expect(username).toHaveAttribute("readonly");
    expect(username).not.toHaveClass("hidden");
  });

  test("refuses a mismatched confirmation without asking the server", async () => {
    let posts = 0;
    server.use(
      statusHandler(false, false, "none"),
      http.post(SETUP_URL, () => {
        posts += 1;
        return HttpResponse.json({
          authenticated: true,
          password_set: true,
          password_source: "file",
        });
      }),
    );
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.type(screen.getByLabelText("Confirm password"), "hunter3");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    expect(
      await screen.findByText("The two passwords don’t match. Type them again."),
    ).toBeInTheDocument();
    // The whole point of the confirm field: a mismatch costs no ~0.16 s scrypt
    // derive, and the route could not have caught it anyway (it takes one
    // password field).
    expect(posts).toBe(0);
    // "Type them again" with focus left on the button means tabbing back to
    // the field first; submitting had disabled that button, so focus was on
    // <body>. The selection is what makes the retype a retype rather than an
    // append. The Account panel's form cites this behaviour as its precedent.
    const confirm =
      screen.getByLabelText<HTMLInputElement>("Confirm password");
    expect(confirm).toHaveFocus();
    expect(confirm.selectionStart).toBe(0);
    expect(confirm.selectionEnd).toBe("hunter3".length);
    // And the message clears the moment either half changes, rather than
    // hanging around contradicting what is now typed.
    await userEvent.type(screen.getByLabelText("Confirm password"), "!");
    expect(
      screen.queryByText("The two passwords don’t match. Type them again."),
    ).not.toBeInTheDocument();
  });

  test("the mismatch clears when the FIRST field is retyped too", async () => {
    // The likelier correction of the two: the user decides the password they
    // meant is the one they typed second. Clearing only on the confirm field
    // left the alert and both red borders standing while they retyped.
    server.use(statusHandler(false, false, "none"));
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.type(screen.getByLabelText("Confirm password"), "hunter3");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    await screen.findByText("The two passwords don’t match. Type them again.");
    await userEvent.type(screen.getByLabelText("Password"), "!");

    expect(
      screen.queryByText("The two passwords don’t match. Type them again."),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Password")).not.toHaveAttribute(
      "aria-invalid",
    );
    expect(screen.getByLabelText("Confirm password")).toHaveAttribute(
      "aria-invalid",
      "false",
    );
  });

  test("marks the confirm field for the mismatch, and no field for a rejection", async () => {
    // `aria-describedby` claims the text is ABOUT this field. The confirm
    // field is the one whose value is wrong relative to the first, and the one
    // the sentence sends the caret to, so it is the one marked — the same rule
    // the Account panel's form follows. The server's rejections are about the
    // request or the server's state, so they are said in the alert and pointed
    // at nothing.
    server.use(
      statusHandler(false, false, "none"),
      http.post(SETUP_URL, () =>
        HttpResponse.json(
          {
            detail:
              "Another sign-in is already in progress. Try again in a moment.",
          },
          { status: 429 },
        ),
      ),
    );
    renderLogin();

    const password = await screen.findByLabelText("Password");
    const confirm = screen.getByLabelText("Confirm password");
    expect(confirm).not.toHaveAttribute("aria-describedby");

    await userEvent.type(password, "hunter2");
    await userEvent.type(confirm, "hunter3");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    const mismatch = await screen.findByRole("alert");
    expect(confirm).toHaveAttribute("aria-describedby", mismatch.id);
    expect(confirm).toHaveAttribute("aria-invalid", "true");
    // NOT the first field: it held half of the mismatched pair, but the
    // sentence is not about the value sitting in it, and the Account panel's
    // form marks only the confirmation for the same mistake.
    expect(password).not.toHaveAttribute("aria-describedby");
    expect(password).not.toHaveAttribute("aria-invalid");

    // Now a matched pair the SERVER refuses: the sentence is about the request,
    // so it is said in the alert and no field is marked.
    await userEvent.type(confirm, "{Backspace}2");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    const rejection = await screen.findByRole("alert");
    expect(rejection).toHaveTextContent("Another sign-in is already");
    expect(password).not.toHaveAttribute("aria-describedby");
    expect(confirm).not.toHaveAttribute("aria-describedby");
    expect(confirm).toHaveAttribute("aria-invalid", "false");
  });

  test("puts focus back in the password field after a rejection", async () => {
    // Submitting disables the button, which drops focus to <body>; the
    // rejection then announces itself to a user whose focus is nowhere, and a
    // keyboard user has to tab from the top of the document to retry.
    server.use(
      statusHandler(false, false, "none"),
      http.post(SETUP_URL, () =>
        HttpResponse.json(
          {
            detail:
              "Another sign-in is already in progress. Try again in a moment.",
          },
          { status: 429 },
        ),
      ),
    );
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.type(screen.getByLabelText("Confirm password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    await screen.findByRole("alert");
    expect(screen.getByLabelText("Password")).toHaveFocus();
  });

  test("a set password signs the browser in, exactly as a sign-in would", async () => {
    server.use(
      statusHandler(false, false, "none"),
      http.post(SETUP_URL, async ({ request }) => {
        expect(await request.json()).toEqual({ password: "hunter2" });
        return HttpResponse.json({
          authenticated: true,
          password_set: true,
          password_source: "file",
        });
      }),
    );
    const { queryClient, invalidate, bump } = renderLogin({
      from: { pathname: "/artists", search: "" },
    });

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.type(screen.getByLabelText("Confirm password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    // Lands on the page that was asked for, with the guard's status query
    // already warm — the setup response IS an AuthStatus, so nothing
    // round-trips again on the way into the shell.
    expect(await screen.findByTestId("location")).toHaveTextContent("/artists");
    expect(queryClient.getQueryData(AUTH_STATUS_KEY)).toEqual({
      authenticated: true,
      password_set: true,
      password_source: "file",
    });
    // The same cache refresh a sign-in does. It matters here for the same
    // reason: a fresh EventSource replays nothing, so anything this browser
    // cached before would sit stale behind the shell.
    expect(invalidate).toHaveBeenCalledTimes(LIBRARY_CONTENT_KEY_COUNT);
    expect(bump).toHaveBeenCalled();
  });

  test("a 409 re-checks, hands over to the sign-in form, and says why", async () => {
    // Someone else set a password while this page was open — another browser,
    // or the operator setting the env var and restarting. Re-asking the server
    // swaps the branch for the sign-in form, which is the only screen that can
    // act on the new password; without a sentence to go with it, the card
    // silently became a different form and the likeliest next move was to type
    // the password they had just chosen.
    let configured = false;
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json({
          authenticated: false,
          password_set: configured,
          password_source: configured ? "file" : "none",
        }),
      ),
      http.post(SETUP_URL, () => {
        configured = true;
        return HttpResponse.json(
          {
            detail:
              "A password is already configured on this server and stored in the beets directory.",
          },
          { status: 409 },
        );
      }),
    );
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.type(screen.getByLabelText("Confirm password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    expect(
      await screen.findByRole("button", { name: "Sign in" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Confirm password")).not.toBeInTheDocument();
    // The sign-in field is `current-password` — the proof this is the OTHER
    // form and not the setup one with a renamed button.
    expect(screen.getByLabelText("Password")).toHaveAttribute(
      "autocomplete",
      "current-password",
    );
    // The account of the swap survives it: the state is held above the branch,
    // so it is still on screen after the form it was raised in unmounted.
    const note = screen.getByText(
      "A password was set from elsewhere while this page was open. Sign in with that one.",
    );
    // In a live region, so it is announced rather than only found by someone
    // already reading the card. `<output>` carries the status role implicitly
    // (the dialect this file already pins for the re-check line).
    expect(note.tagName).toBe("OUTPUT");
    expect(note).toBe(screen.getByRole("status"));
    // The header follows the branch too — this is a sign-in screen now.
    expect(
      screen.getByRole("heading", { level: 2, name: "Sign in" }),
    ).toBeInTheDocument();
  });

  test("a 409 whose re-check fails still says what happened, and moves focus", async () => {
    // The modal reason a 409 arrives at all is a server that was just
    // restarted with a password — so the re-check right behind it is the
    // request most likely to fail. It leaves `password_set: false` in the
    // cache, so this form stays mounted: the sentence has to be visible HERE
    // too, or the click changed nothing observable on the page.
    let statusCalls = 0;
    server.use(
      http.get(STATUS_URL, () => {
        statusCalls += 1;
        return statusCalls === 1
          ? HttpResponse.json({
              authenticated: false,
              password_set: false,
              password_source: "none",
            })
          : HttpResponse.error();
      }),
      http.post(SETUP_URL, () =>
        HttpResponse.json(
          {
            detail:
              "A password is already configured on this server and stored in the beets directory.",
          },
          { status: 409 },
        ),
      ),
    );
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.type(screen.getByLabelText("Confirm password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    const note = await screen.findByText(
      "A password was set from elsewhere while this page was open. Sign in with that one.",
    );
    expect(note).toBe(screen.getByRole("status"));
    await waitFor(() => expect(statusCalls).toBe(2));
    // Still the setup form — and focus is in it, not on the <body> the
    // disabled button dropped it to.
    expect(
      screen.getByRole("button", { name: "Set password" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveFocus();
  });

  test("the last answer's sentence does not outlive the next submit", async () => {
    // The rejection was read off the mutation's error, so a 409 — which
    // deliberately says nothing itself — left the PREVIOUS answer's alert
    // standing under the note explaining the swap. Reachable in one sitting: a
    // sign-in already in flight (429), then someone else sets the password.
    let posts = 0;
    let statusCalls = 0;
    server.use(
      http.get(STATUS_URL, () => {
        statusCalls += 1;
        return statusCalls === 1
          ? HttpResponse.json({
              authenticated: false,
              password_set: false,
              password_source: "none",
            })
          : HttpResponse.error();
      }),
      http.post(SETUP_URL, () => {
        posts += 1;
        return posts === 1
          ? HttpResponse.json(
              {
                detail:
                  "Another sign-in is already in progress. Try again in a moment.",
              },
              { status: 429 },
            )
          : HttpResponse.json(
              {
                detail:
                  "A password is already configured on this server and stored in the beets directory.",
              },
              { status: 409 },
            );
      }),
    );
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.type(screen.getByLabelText("Confirm password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Another sign-in is already",
    );

    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    await screen.findByText(
      "A password was set from elsewhere while this page was open. Sign in with that one.",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test.each([
    [422, "A password cannot be empty or only whitespace."],
    [
      503,
      "The new password could not be saved to the beets directory, so nothing was changed. Check that the directory is writable, then try again.",
    ],
    [429, "Another sign-in is already in progress. Try again in a moment."],
  ])("renders the %i detail as written, keeping the form", async (status, detail) => {
    // The server authors each of these and names its own cause; a re-worded
    // copy here could only drift from it. The form stays: a 429 means "try that
    // again", and a 503 is fixed outside the browser and then retried.
    server.use(
      statusHandler(false, false, "none"),
      http.post(SETUP_URL, () => HttpResponse.json({ detail }, { status })),
    );
    renderLogin();

    await userEvent.type(await screen.findByLabelText("Password"), "  ");
    await userEvent.type(screen.getByLabelText("Confirm password"), "  ");
    await userEvent.click(screen.getByRole("button", { name: "Set password" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(
      screen.getByRole("button", { name: "Set password" }),
    ).toBeEnabled();
  });
});

describe("LoginPage — an unreadable MUSICDROP_PASSWORD_HASH", () => {
  test("names the variable and the compose rule — and offers NO setup", async () => {
    server.use(statusHandler(false, false, "env"));
    renderLogin();

    const banner = await screen.findByRole("alert");
    // In the system's shape for an attention-demanding status, not muted prose
    // wearing role="alert".
    expect(banner).toHaveAttribute("data-slot", "status-banner");
    expect(banner).toHaveTextContent("MUSICDROP_PASSWORD_HASH");
    // The measured way this state is reached: docker-compose interpolates a
    // single $ followed by a letter, a digit or _, so a hash pasted straight
    // from the CLI usually arrives shorter than it left (11 of 12 real hashes
    // in the probe). Without this sentence the operator is told their hash is
    // wrong and not why.
    expect(banner).toHaveTextContent("must be doubled to");
    // No setup form, and this is the owner's ruling rather than an oversight:
    // the env var wins even when its value cannot be read, so a password set
    // here would be shadowed the moment the variable is corrected.
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Set password" }),
    ).not.toBeInTheDocument();
    // Nobody can sign in here either, but this branch is about a variable the
    // operator has to fix in a shell — the card keeps the sign-in title.
    expect(
      screen.getByRole("heading", { level: 2, name: "Sign in" }),
    ).toBeInTheDocument();
  });

  test("offers the hash command, since fixing the variable needs a new one", async () => {
    server.use(statusHandler(false, false, "env"));
    renderLogin();

    // Both invocations from README §Authentication — the container one first.
    const commands = await screen.findByText(/hash_password/);
    expect(commands).toHaveTextContent(
      "docker exec -it musicdrop python -m app.auth.hash_password",
    );
    expect(commands).toHaveTextContent(
      "cd backend && uv run python -m app.auth.hash_password",
    );
  });

  test("connects the command to the fix, and names both outcomes of unsetting", async () => {
    // The banner said "fix or unset it" and the block said "generate", with
    // nothing joining them: the operator had to work out that fixing MEANS
    // generating a fresh hash and pasting it with doubled dollars.
    server.use(statusHandler(false, false, "env"));
    renderLogin();

    const instruction = await screen.findByText(/paste it into the variable/i);
    expect(instruction).toHaveTextContent(/restart MusicDrop/i);
    // Both outcomes, because this page cannot tell them apart: status reports
    // `password_source: "env"` and says nothing about a stored password sitting
    // behind it. Claiming "this screen sets the password instead" was false on
    // a server that also has one stored.
    expect(instruction).toHaveTextContent(/if a password is stored/i);
    expect(instruction).toHaveTextContent(/otherwise this screen sets a new one/i);
    // Beside the command it introduces, not stranded above the banner.
    const snippet = screen.getByText(/docker exec -it musicdrop/);
    expect(instruction.compareDocumentPosition(snippet)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  test("copies the commands when the clipboard allows it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    server.use(statusHandler(false, false, "env"));
    renderLogin();

    await userEvent.click(await screen.findByRole("button", { name: "Copy" }));

    expect(
      await screen.findByRole("button", { name: "Copied" }),
    ).toBeInTheDocument();
    expect(writeText.mock.calls[0][0]).toContain("app.auth.hash_password");
  });

  test("the command block shows both commands in full, with no scroll container", async () => {
    // This was `overflow-x-auto` + tabIndex + role="group": a scroll container
    // only a mouse can reach fails WCAG 2.1.1, so it needed a named tab stop.
    // It wraps now, so there is nothing to scroll to and no stop to name. That
    // matters most HERE: the sign-in card is max-w-sm at every viewport, so
    // the old version truncated this command on a 4K monitor too.
    server.use(statusHandler(false, false, "env"));
    renderLogin();

    const block = await screen.findByText(/docker exec -it musicdrop/);
    expect(block.tagName).toBe("PRE");
    expect(block.className).toContain("whitespace-pre-wrap");
    expect(block.className).not.toContain("overflow-x");
    expect(block).not.toHaveAttribute("tabindex");
    // BOTH commands, in full. The checkout one is the half that scrolling hid.
    expect(block).toHaveTextContent("app.auth.hash_password");
    expect(block).toHaveTextContent("cd backend && uv run python");
  });

  // The re-check lives on the two notice branches, which are the ones that end
  // in "restart MusicDrop" and have no other action to offer. (These four
  // moved here from the first-run describe when the setup branch dropped the
  // button; the component under test is the same `RecheckStatus`.)
  test("a re-check that changes nothing names the variable that is still wrong", async () => {
    // A silent re-check is indistinguishable from a dead button, and this
    // branch is exactly where an operator clicks it repeatedly while waiting
    // for a restart to change the answer.
    server.use(statusHandler(false, false, "env"));
    renderLogin();

    await screen.findByRole("button", { name: "Check again" });
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    // Not "Still no password configured" — one IS configured, and saying
    // otherwise would send the operator looking for the wrong problem.
    const notice = await screen.findByText(
      "The hash in MUSICDROP_PASSWORD_HASH is still unreadable.",
    );
    // The element is `<output>`, not `<p role="status">`. Pinned because the
    // two are interchangeable to every other assertion in this file, so the
    // swap would otherwise be untested: `<output>` has an IMPLICIT status
    // role, and this asserts the live region survived the change.
    expect(notice.tagName).toBe("OUTPUT");
    expect(notice).toBe(screen.getByRole("status"));
    expect(
      await screen.findByRole("button", { name: "Check again" }),
    ).toBeEnabled();
  });

  test("re-checks on demand, so a password set elsewhere is picked up", async () => {
    let configured = false;
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json({
          authenticated: false,
          password_set: configured,
          password_source: configured ? "file" : "env",
        }),
      ),
    );
    renderLogin();

    await screen.findByRole("button", { name: "Check again" });
    configured = true;
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    expect(
      await screen.findByRole("button", { name: "Sign in" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("MUSICDROP_PASSWORD_HASH")).not.toBeInTheDocument();
  });

  test("shows the re-check in progress, so the click is not silent", async () => {
    // `refetch` leaves `isPending` false (v5: initial load only), so a button
    // wired to that reports nothing at all. The handler is held open here to
    // make the in-flight moment observable rather than a race.
    let calls = 0;
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(STATUS_URL, async () => {
        calls += 1;
        if (calls > 1) await held;
        return HttpResponse.json({
          authenticated: false,
          password_set: false,
          password_source: "env",
        });
      }),
    );
    renderLogin();

    await screen.findByRole("button", { name: "Check again" });
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    const busy = await screen.findByRole("button", { name: "Checking…" });
    expect(busy).toBeDisabled();

    release();
    expect(
      await screen.findByRole("button", { name: "Check again" }),
    ).toBeEnabled();
  });

  test("a re-check against an unreachable server says THAT instead", async () => {
    let up = true;
    server.use(
      http.get(STATUS_URL, () =>
        up
          ? HttpResponse.json({
              authenticated: false,
              password_set: false,
              password_source: "env",
            })
          : new HttpResponse(null, { status: 503 }),
      ),
    );
    renderLogin();

    await screen.findByRole("button", { name: "Check again" });
    up = false;
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    // "Still unreadable" would be a guess here — the server never said.
    expect(
      await screen.findByText(
        "Couldn’t reach the server — it may still be restarting.",
      ),
    ).toBeInTheDocument();
  });
});

describe("LoginPage — an unreadable stored password", () => {
  test("says what to delete, and does not offer to overwrite it", async () => {
    server.use(statusHandler(false, false, "file"));
    renderLogin();

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveAttribute("data-slot", "status-banner");
    expect(banner).toHaveTextContent("password-hash");
    expect(banner).toHaveTextContent(/delete/i);
    expect(banner).toHaveTextContent(/restart MusicDrop/i);
    // Setup is not offered: the server refuses to overwrite a hash file it
    // cannot read, so a form here would collect a password and be refused.
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    // And the env var is not named — it is not what is wrong here, and this
    // branch exists precisely because the two used to be told the same story.
    expect(screen.queryByText("MUSICDROP_PASSWORD_HASH")).not.toBeInTheDocument();
    // The card keeps the sign-in title: the recovery here is deleting a file
    // and restarting, not setting a password on this screen.
    expect(
      screen.getByRole("heading", { level: 2, name: "Sign in" }),
    ).toBeInTheDocument();
  });

  test("a re-check that changes nothing says the stored one is still unreadable", async () => {
    server.use(statusHandler(false, false, "file"));
    renderLogin();

    await screen.findByRole("button", { name: "Check again" });
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    expect(
      await screen.findByText("The stored password is still unreadable."),
    ).toBeInTheDocument();
  });
});
