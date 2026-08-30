import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, test, vi } from "vitest";

import * as assetVersion from "@/api/assetVersion";
import { AUTH_STATUS_KEY } from "@/api/auth";
import { markAuthenticated } from "@/api/authStore";
import { LIBRARY_CONTENT_KEY_COUNT } from "@/api/useEventStream";
import { LoginPage } from "@/pages/LoginPage";
import { server } from "@/test/msw-server";

const STATUS_URL = `${window.location.origin}/api/auth/status`;
const LOGIN_URL = `${window.location.origin}/api/auth/login`;

/** The two states GET /api/auth/status can report to a signed-out browser. */
function statusHandler(passwordSet: boolean, authenticated = false) {
  return http.get(STATUS_URL, () =>
    HttpResponse.json({ authenticated, password_set: passwordSet }),
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
  test.each([
    [401, "incorrect password"],
    [401, "no password is configured on this server"],
    [401, "the configured password hash is not readable"],
    [429, "another sign-in attempt is in progress"],
    [503, "the session signing secret is unavailable"],
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
              { detail: "another sign-in attempt is in progress" },
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
      "another sign-in attempt is in progress",
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
        HttpResponse.json({ detail: "incorrect password" }, { status: 401 }),
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

  test("refuses a destination that would leave the app", async () => {
    // The remembered location is read back as `unknown`: it only has to LOOK
    // like a Location. `navigate()` follows an absolute URL, so a `from` that
    // is not rooted at "/" must fall back to the Overview rather than send the
    // user somewhere else entirely.
    server.use(
      statusHandler(true),
      http.post(LOGIN_URL, () =>
        HttpResponse.json({ authenticated: true, password_set: true }),
      ),
    );
    renderLogin({ from: { pathname: "https://example.invalid/steal", search: "" } });

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("shell stand-in")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/");
    expect(screen.getByTestId("location")).not.toHaveTextContent("example");
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

describe("LoginPage — no password configured", () => {
  test("explains the setup instead of offering a doomed form", async () => {
    server.use(statusHandler(false));
    renderLogin();

    expect(
      await screen.findByText(/no password is configured on this server/i),
    ).toBeInTheDocument();
    // The form would 401 on every attempt; showing it would waste the
    // operator's time on the wrong problem.
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    // Both invocations from README §Authentication — the container one first.
    const commands = screen.getByText(/hash_password/);
    expect(commands).toHaveTextContent(
      "docker exec -it musicdrop python -m app.auth.hash_password",
    );
    expect(commands).toHaveTextContent(
      "cd backend && uv run python -m app.auth.hash_password",
    );
    // The env var the generated hash goes into — the second half of the fix,
    // and the reason the copy is a paragraph rather than just a command.
    expect(screen.getByText("MUSICDROP_PASSWORD_HASH")).toBeInTheDocument();
  });

  test("re-checks on demand, so a restart is picked up without a reload", async () => {
    let configured = false;
    server.use(
      http.get(STATUS_URL, () =>
        HttpResponse.json({ authenticated: false, password_set: configured }),
      ),
    );
    renderLogin();

    await screen.findByText(/no password is configured on this server/i);
    configured = true;
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));

    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
    expect(
      screen.queryByText(/no password is configured on this server/i),
    ).not.toBeInTheDocument();
  });

  test("copies the commands when the clipboard allows it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    server.use(statusHandler(false));
    renderLogin();

    await userEvent.click(await screen.findByRole("button", { name: "Copy" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument(),
    );
    expect(writeText.mock.calls[0][0]).toContain("app.auth.hash_password");
    vi.unstubAllGlobals();
  });
});
