import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, test } from "vitest";

import type { PasswordSource } from "@/api/auth";
import { markAuthenticated } from "@/api/authStore";
import { AccountPanel } from "@/pages/settings/AccountPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const STATUS_URL = `${window.location.origin}/api/auth/status`;
const CHANGE_URL = `${window.location.origin}/api/auth/password`;

/** A signed-in browser looking at a server whose password comes from `source`.
 * The panel branches on `password_source` alone — `password_set` is true in
 * every case it renders a form for, because you cannot be signed in otherwise. */
function statusHandler(source: PasswordSource) {
  return http.get(STATUS_URL, () =>
    HttpResponse.json({
      authenticated: true,
      password_set: true,
      password_source: source,
    }),
  );
}

/**
 * The StatusBanner whose prose contains `text`.
 *
 * Not `getByRole("status")`: this panel mounts a PERMANENT polite region for
 * the success line (empty and sr-only until there is one), and the loading
 * branch is an `<output>`, so the role is ambiguous exactly when a banner is on
 * screen. Matching the prose first and walking up to `data-slot` asks the
 * question that matters — is this sentence inside the system's banner, or is it
 * loose text wearing a live-region role.
 */
async function findBanner(text: RegExp): Promise<HTMLElement> {
  const prose = await screen.findByText(text);
  const banner = prose.closest("[data-slot='status-banner']");
  expect(banner).toBeInstanceOf(HTMLElement);
  return banner as HTMLElement;
}

/** Fills all three fields and submits. */
async function submitChange(current: string, next: string, confirm = next) {
  await userEvent.type(
    await screen.findByLabelText("Current password"),
    current,
  );
  await userEvent.type(screen.getByLabelText("New password"), next);
  await userEvent.type(screen.getByLabelText("Confirm new password"), confirm);
  await userEvent.click(
    screen.getByRole("button", { name: "Change password" }),
  );
}

beforeEach(() => {
  markAuthenticated();
});

describe("AccountPanel — the form", () => {
  test("offers current / new / confirm, wired for a password manager", async () => {
    server.use(statusHandler("file"));
    renderWithProviders(<AccountPanel />);

    const current = await screen.findByLabelText("Current password");
    // `current-password` on the field that verifies, `new-password` on the two
    // that replace: a manager should offer the saved entry for the first and
    // propose a fresh one for the others.
    expect(current).toHaveAttribute("autocomplete", "current-password");
    expect(current).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("New password")).toHaveAttribute(
      "autocomplete",
      "new-password",
    );
    expect(screen.getByLabelText("Confirm new password")).toHaveAttribute(
      "autocomplete",
      "new-password",
    );
  });

  test("carries the username field password managers look for", async () => {
    // Same reasoning as the sign-in screen's forms: a change-password form
    // with password fields alone is the shape Chrome warns about and the shape
    // a manager files awkwardly. Off screen, not a tab stop, not announced.
    server.use(statusHandler("file"));
    renderWithProviders(<AccountPanel />);

    const current = await screen.findByLabelText("Current password");
    const username = current
      .closest("form")
      ?.querySelector('input[autocomplete="username"]');
    expect(username).toBeInstanceOf(HTMLInputElement);
    expect(username).toHaveAttribute("tabindex", "-1");
    expect(username).toHaveAttribute("aria-hidden", "true");
  });

  test("the panel says how to recover a password nobody remembers", async () => {
    // This screen is the only one that can say it while the operator can still
    // sign in; the sign-in screen only gets to say it once they cannot.
    server.use(statusHandler("file"));
    renderWithProviders(<AccountPanel />);

    // Wait for the FORM, not for the region: the region exists from the first
    // paint (the loading branch is inside the same panel), so asserting on it
    // any earlier only reads the probe's own line.
    await screen.findByLabelText("Current password");
    const panel = screen.getByRole("region", { name: "Password" });
    expect(panel).toHaveTextContent("password-hash");
    expect(panel).toHaveTextContent(/delete/i);
    expect(panel).toHaveTextContent(/restart MusicDrop/i);
  });

  test("the recovery is a footnote under the form, not the panel's lede", async () => {
    // It is an aside — the thing you read once, months before you need it —
    // and in the lede it was the tallest thing in the panel on a phone, above
    // the three fields it is not about.
    server.use(statusHandler("file"));
    renderWithProviders(<AccountPanel />);

    // The form first: the lede is on screen from the first paint, while the
    // probe is still out, so reading it any earlier proves nothing about the
    // branch that renders the fields.
    const current = await screen.findByLabelText("Current password");
    const lede = screen.getByText(/protected by a single password/i);
    expect(lede).not.toHaveTextContent(/delete/i);
    const footnote = screen.getByText(/Forgot it\?/);
    expect(footnote).toHaveTextContent("password-hash");
    // Under the form it belongs to: the fields come first.
    expect(footnote.compareDocumentPosition(current)).toBe(
      Node.DOCUMENT_POSITION_PRECEDING,
    );
  });

  test("a change says the password moved AND that other sessions were dropped", async () => {
    server.use(
      statusHandler("file"),
      http.post(CHANGE_URL, async ({ request }) => {
        expect(await request.json()).toEqual({
          current_password: "old-one",
          new_password: "new-one",
        });
        return HttpResponse.json({
          authenticated: true,
          password_set: true,
          password_source: "file",
        });
      }),
    );
    renderWithProviders(<AccountPanel />);

    await submitChange("old-one", "new-one");

    // Not a warning to design away: the signing key is derived from the
    // password hash, so this is the ONLY revocation this stateless session
    // design has, and an operator changing the password because a cookie leaked
    // needs to know it worked.
    const done = await screen.findByRole("status");
    expect(done).toHaveTextContent("Password changed.");
    expect(done).toHaveTextContent(/every other signed-in browser was signed out/i);
    // Every field held a secret that is now stale — the old password is wrong
    // and the new one is saved.
    expect(screen.getByLabelText("Current password")).toHaveValue("");
    expect(screen.getByLabelText("New password")).toHaveValue("");
    expect(screen.getByLabelText("Confirm new password")).toHaveValue("");
    // And focus has somewhere to be: submitting disabled the button, which
    // drops focus to <body>, and all three fields were then emptied — so
    // after a SUCCESSFUL change nothing at all was focused.
    expect(screen.getByLabelText("Current password")).toHaveFocus();
  });

  test("a mismatch puts the caret where the sentence says to type", async () => {
    // "Type them again" with focus left on the button means tabbing back to
    // the field first. The sign-in screen's setup form already focuses and
    // selects the confirm field on the same mistake.
    server.use(statusHandler("file"));
    renderWithProviders(<AccountPanel />);

    await submitChange("old-one", "new-one", "new-two");

    await screen.findByText("The two passwords don’t match. Type them again.");
    const confirm =
      screen.getByLabelText<HTMLInputElement>("Confirm new password");
    expect(confirm).toHaveFocus();
    expect(confirm.selectionStart).toBe(0);
    expect(confirm.selectionEnd).toBe("new-two".length);
  });

  test("the mismatch clears when the NEW password field is retyped", async () => {
    // It was cleared only by the confirm field's own edit, so the alert and
    // both red borders stood while the user retyped the other half.
    server.use(statusHandler("file"));
    renderWithProviders(<AccountPanel />);

    await submitChange("old-one", "new-one", "new-two");
    await screen.findByText("The two passwords don’t match. Type them again.");

    await userEvent.type(screen.getByLabelText("New password"), "!");

    expect(
      screen.queryByText("The two passwords don’t match. Type them again."),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Confirm new password")).toHaveAttribute(
      "aria-invalid",
      "false",
    );
  });

  test("a mismatched confirmation is caught here, not by a scrypt derive", async () => {
    let posts = 0;
    server.use(
      statusHandler("file"),
      http.post(CHANGE_URL, () => {
        posts += 1;
        return HttpResponse.json({
          authenticated: true,
          password_set: true,
          password_source: "file",
        });
      }),
    );
    renderWithProviders(<AccountPanel />);

    await submitChange("old-one", "new-one", "new-two");

    expect(
      await screen.findByText("The two passwords don’t match. Type them again."),
    ).toBeInTheDocument();
    // The route takes ONE new password, so it could not have caught this — and
    // asking it would spend a ~0.16 s derive to be told nothing.
    expect(posts).toBe(0);
  });
});

describe("AccountPanel — what the server refuses", () => {
  test("a wrong current password keeps the form and says so, without signing anyone out", async () => {
    server.use(
      statusHandler("file"),
      http.post(CHANGE_URL, () =>
        HttpResponse.json(
          { detail: "The current password is incorrect." },
          // 403 and deliberately NOT 401: this panel lives inside the shell, so
          // a 401 on a gated path is what the client reads as "your session
          // ended" — a typo would sign the operator out mid-correction.
          { status: 403 },
        ),
      ),
    );
    renderWithProviders(<AccountPanel />);

    await submitChange("wrong-one", "new-one");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The current password is incorrect.",
    );
    // The form is still here and still usable — the whole point of the 403.
    const current = screen.getByLabelText("Current password");
    expect(current).toBeEnabled();
    expect(current).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("New password")).toHaveValue("new-one");
    expect(
      screen.getByRole("button", { name: "Change password" }),
    ).toBeEnabled();
    // Focus went back to the field that was wrong: submitting disabled the
    // button, which drops focus to <body>.
    expect(current).toHaveFocus();
    // The rejection is about the current password, so the field it describes is
    // that one — the confirm field is described by the mismatch or by nothing.
    const alert = screen.getByRole("alert");
    expect(current).toHaveAttribute("aria-describedby", alert.id);
    expect(
      screen.getByLabelText("Confirm new password"),
    ).not.toHaveAttribute("aria-describedby");
    // Feedback sits in the action row with the button, as the Plex and slskd
    // panels do, rather than stacked under it.
    expect(alert.parentElement).toContainElement(
      screen.getByRole("button", { name: "Change password" }),
    );
  });

  test("the mismatch describes BOTH fields, since it is about the pair", async () => {
    server.use(statusHandler("file"));
    renderWithProviders(<AccountPanel />);

    const confirm = await screen.findByLabelText("Confirm new password");
    expect(confirm).not.toHaveAttribute("aria-describedby");

    await submitChange("old-one", "new-one", "new-two");

    const mismatch = await screen.findByRole("alert");
    expect(confirm).toHaveAttribute("aria-describedby", mismatch.id);
    expect(screen.getByLabelText("Current password")).toHaveAttribute(
      "aria-describedby",
      mismatch.id,
    );
  });

  test("a 409 reads as a notice about the server, not as a field error", async () => {
    // Not reachable while the panel hides the form under "env", and handled
    // anyway: the status this panel read is a cached answer, and a restart can
    // invalidate it under a tab that was left open.
    server.use(
      statusHandler("file"),
      http.post(CHANGE_URL, () =>
        HttpResponse.json(
          {
            detail:
              "The password comes from MUSICDROP_PASSWORD_HASH, which overrides the stored one, so it cannot be changed here.",
          },
          { status: 409 },
        ),
      ),
    );
    renderWithProviders(<AccountPanel />);

    await submitChange("old-one", "new-one");

    const banner = await findBanner(/overrides the stored one/i);
    expect(banner).toHaveAttribute("role", "status");
    expect(banner).toHaveTextContent("MUSICDROP_PASSWORD_HASH");
    // Carries a glyph, like the other neutral banners in the app.
    expect(banner.querySelector("svg")).toBeInTheDocument();
    // Nothing painted red, and nothing pointed at a field: the user typed
    // nothing wrong.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test.each([
    [422, "A password cannot be empty or only whitespace."],
    [
      503,
      "The new password could not be saved to the beets directory, so nothing was changed. Check that the directory is writable, then try again.",
    ],
  ])("renders the %i detail as written", async (status, detail) => {
    server.use(
      statusHandler("file"),
      http.post(CHANGE_URL, () => HttpResponse.json({ detail }, { status })),
    );
    renderWithProviders(<AccountPanel />);

    await submitChange("old-one", "  ");

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  });

  test("a rejection that is not about a field still leaves focus in the form", async () => {
    // 429 and 503 both mean "try that again", and the button that was clicked
    // is disabled while the request is out, so focus was on <body> when the
    // sentence asking for a retry arrived. The first field is where the retry
    // starts; nothing is selected, because nothing typed here was wrong.
    server.use(
      statusHandler("file"),
      http.post(CHANGE_URL, () =>
        HttpResponse.json(
          {
            detail:
              "Another sign-in is already in progress. Try again in a moment.",
          },
          { status: 429 },
        ),
      ),
    );
    renderWithProviders(<AccountPanel />);

    await submitChange("old-one", "new-one");

    await screen.findByRole("alert");
    const current =
      screen.getByLabelText<HTMLInputElement>("Current password");
    expect(current).toHaveFocus();
    expect(current.selectionStart).toBe(current.selectionEnd);
    // Not painted red either: the value in it is not what the server refused.
    expect(current).toHaveAttribute("aria-invalid", "false");
  });
});

describe("AccountPanel — under the environment override", () => {
  test("explains the override and how to hand control over, with no form", async () => {
    server.use(statusHandler("env"));
    renderWithProviders(<AccountPanel />);

    const notice = await findBanner(/overrides any password stored by the app/i);
    // Neutral, not a warning: nothing is broken, the app simply is not the
    // owner of this credential — so it gets role="status", not role="alert".
    expect(notice).toHaveAttribute("role", "status");
    expect(notice).toHaveTextContent("MUSICDROP_PASSWORD_HASH");
    expect(notice).toHaveTextContent(/unset that variable/i);
    expect(notice).toHaveTextContent(/restart MusicDrop/i);
    // A form here would be a dead control — the route answers 409 whatever is
    // typed — and hiding the panel outright would leave the operator with no
    // way to learn WHY the app will not manage their password.
    expect(
      screen.queryByLabelText("Current password"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Change password" }),
    ).not.toBeInTheDocument();
  });

  test("does not repeat the delete-the-file recovery, which is WRONG here", async () => {
    // Caught in the browser pass, not by a test: the panel's default
    // description tells the operator to delete the password-hash file, and
    // under the override that is not merely incomplete — the variable wins
    // whether or not a file is there, so deleting one changes nothing and sends
    // a locked-out operator to do the one thing that cannot help.
    server.use(statusHandler("env"));
    renderWithProviders(<AccountPanel />);

    // Wait for the notice, not for the region: the region exists from the first
    // paint (the loading branch is inside the same panel) and still carries the
    // default description then, so asserting on it too early passes vacuously.
    await findBanner(/overrides any password stored by the app/i);
    const panel = screen.getByRole("region", { name: "Password" });
    expect(panel).not.toHaveTextContent(
      /delete the password-hash file in MusicDrop’s beets directory/,
    );
    expect(screen.queryByText(/Forgot it\?/)).not.toBeInTheDocument();
    // The recovery that IS true here, said once.
    expect(panel).toHaveTextContent(/unset that variable/i);
  });

  test("names both outcomes of unsetting, because it cannot know which", async () => {
    // Measured on a scratch server that had BOTH: after unsetting the
    // variable, the password-hash FILE took over and the sign-in screen asked
    // for that password — so "the sign-in screen will then set a new one" was
    // false. Status reports `password_source: "env"` and nothing about what is
    // shadowed behind it, so the honest sentence covers both.
    server.use(statusHandler("env"));
    renderWithProviders(<AccountPanel />);

    const notice = await findBanner(/overrides any password stored by the app/i);
    expect(notice).toHaveTextContent(/if a password is stored on this server/i);
    expect(notice).toHaveTextContent(
      /otherwise the sign-in screen sets a new one/i,
    );
    expect(notice).not.toHaveTextContent(
      "the sign-in screen will then set a new one",
    );
    // A glyph, like every other neutral banner in the app: colour alone does
    // not carry the state, and a text-only box reads as a quoted paragraph
    // rather than as a notice from the system.
    expect(notice.querySelector("svg")).toBeInTheDocument();
  });
});

describe("AccountPanel — before and instead of an answer", () => {
  test("shows neither the form nor the override notice while the probe is out", async () => {
    server.use(statusHandler("env"));
    renderWithProviders(<AccountPanel />);

    // Either branch rendered early would be a claim about a server that has not
    // answered yet — and the two say opposite things.
    expect(
      screen.queryByLabelText("Current password"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("MUSICDROP_PASSWORD_HASH")).not.toBeInTheDocument();
    // What IS on screen meanwhile: the probe saying so, rather than an empty
    // panel that reads as a section with nothing in it.
    expect(screen.getByRole("status")).toHaveTextContent(
      "Checking how this server’s password is configured…",
    );

    expect(
      await findBanner(/overrides any password stored by the app/i),
    ).toHaveTextContent("MUSICDROP_PASSWORD_HASH");
  });

  test("a failed probe offers a retry rather than a form it cannot vouch for", async () => {
    let up = false;
    server.use(
      http.get(STATUS_URL, () =>
        up
          ? HttpResponse.json({
              authenticated: true,
              password_set: true,
              password_source: "file",
            })
          : new HttpResponse(null, { status: 503 }),
      ),
    );
    renderWithProviders(<AccountPanel />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn’t check how this server’s password is configured.",
    );
    up = true;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(
      await screen.findByLabelText("Current password"),
    ).toBeInTheDocument();
  });
});
