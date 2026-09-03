import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
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
  // The server's own sentences, verbatim (backend app/auth/routes.py) — these
  // are the strings the UI must not paraphrase, so the fixture has to BE them.
  test.each([
    [401, "Incorrect password."],
    [401, "No password is configured on this server."],
    [401, "The configured password hash is not readable."],
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
    // And the message clears the moment either half changes, rather than
    // hanging around contradicting what is now typed.
    await userEvent.type(screen.getByLabelText("Confirm password"), "!");
    expect(
      screen.queryByText("The two passwords don’t match. Type them again."),
    ).not.toBeInTheDocument();
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

  test("a 409 re-checks and hands over to the sign-in form", async () => {
    // Someone else set a password while this page was open — another browser,
    // or the operator setting the env var and restarting. Narrating the 409
    // would leave them reading a sentence about a form that is no longer the
    // one they need; re-asking the server swaps the branch for the sign-in
    // form, which is the only screen that can act on the new password.
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

  test("re-checks on demand, so a password set elsewhere is picked up", async () => {
    let configured = false;
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json({
          authenticated: false,
          password_set: configured,
          password_source: configured ? "file" : "none",
        }),
      ),
    );
    renderLogin();

    await screen.findByRole("button", { name: "Set password" });
    configured = true;
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    expect(
      await screen.findByRole("button", { name: "Sign in" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Confirm password")).not.toBeInTheDocument();
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
          password_source: "none",
        });
      }),
    );
    renderLogin();

    await screen.findByRole("button", { name: "Set password" });
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    const busy = await screen.findByRole("button", { name: "Checking…" });
    expect(busy).toBeDisabled();

    release();
    expect(
      await screen.findByRole("button", { name: "Check again" }),
    ).toBeEnabled();
  });

  test("re-checking says so when nothing changed, instead of nothing", async () => {
    // A silent re-check is indistinguishable from a dead button, and this
    // branch is exactly where an operator clicks it repeatedly while waiting
    // for another browser (or a restart) to change the answer.
    server.use(statusHandler(false, false, "none"));
    renderLogin();

    await screen.findByRole("button", { name: "Set password" });
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    const notice = await screen.findByText("Still no password configured.");
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

  test("a re-check against an unreachable server says THAT instead", async () => {
    let up = true;
    server.use(
      http.get(STATUS_URL, () =>
        up
          ? HttpResponse.json({
              authenticated: false,
              password_set: false,
              password_source: "none",
            })
          : new HttpResponse(null, { status: 503 }),
      ),
    );
    renderLogin();

    await screen.findByRole("button", { name: "Set password" });
    up = false;
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    // "Still not configured" would be a guess here — the server never said.
    expect(
      await screen.findByText(
        "Couldn’t reach the server — it may still be restarting.",
      ),
    ).toBeInTheDocument();
  });
});

describe("LoginPage — an unreadable MUSICDROP_PASSWORD_HASH", () => {
  test("names the variable, the compose rule, and the restart — and offers NO setup", async () => {
    server.use(statusHandler(false, false, "env"));
    renderLogin();

    const banner = await screen.findByRole("alert");
    // In the system's shape for an attention-demanding status, not muted prose
    // wearing role="alert".
    expect(banner).toHaveAttribute("data-slot", "status-banner");
    expect(banner).toHaveTextContent("MUSICDROP_PASSWORD_HASH");
    // The measured way this state is reached: docker-compose interpolates a
    // single $, so a hash pasted straight from the CLI arrives shorter than it
    // left. Without this sentence the operator is told their hash is wrong and
    // not why.
    expect(banner).toHaveTextContent("must be doubled to");
    expect(banner).toHaveTextContent(/restart MusicDrop/i);
    // No setup form, and this is the owner's ruling rather than an oversight:
    // the env var wins even when its value cannot be read, so a password set
    // here would be shadowed the moment the variable is corrected.
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Set password" }),
    ).not.toBeInTheDocument();
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

  test("a re-check that changes nothing names the variable that is still wrong", async () => {
    server.use(statusHandler(false, false, "env"));
    renderLogin();

    await screen.findByRole("button", { name: "Check again" });
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    // Not "Still no password configured" — one IS configured, and saying
    // otherwise would send the operator looking for the wrong problem.
    expect(
      await screen.findByText(
        "The hash in MUSICDROP_PASSWORD_HASH is still unreadable.",
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
