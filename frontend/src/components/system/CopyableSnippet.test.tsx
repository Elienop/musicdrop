import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";

import { CopyableSnippet } from "@/components/system/CopyableSnippet";

const SNIPPET = "docker exec -it musicdrop python -m app.auth.hash_password";

/** Captured before any test stubs it. Restoring ONLY navigator, because
 * `vi.unstubAllGlobals()` would also drop test/setup.ts's own stubs
 * (scrollTo, matchMedia, EventSource) for the rest of this file. */
const REAL_NAVIGATOR = globalThis.navigator;

afterEach(() => {
  vi.stubGlobal("navigator", REAL_NAVIGATOR);
});

function stubClipboard(writeText: () => Promise<void>) {
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
}

describe("CopyableSnippet", () => {
  test("shows the text and copies it verbatim, confirming on the button", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard(writeText);
    render(<CopyableSnippet label="Generate a password hash" snippet={SNIPPET} />);

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument(),
    );
    // Verbatim: a snippet the user is told to paste elsewhere must not be
    // reformatted on its way to the clipboard.
    expect(writeText).toHaveBeenCalledWith(SNIPPET);
  });

  test("a refused clipboard is a no-op, because the text is still on screen", async () => {
    // Clipboard access is denied in an insecure context — which is exactly how
    // a self-hosted app is often reached (plain http on a LAN address).
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    stubClipboard(writeText);
    render(<CopyableSnippet label="Generate a password hash" snippet={SNIPPET} />);

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copied" })).not.toBeInTheDocument();
    expect(screen.getByText(SNIPPET)).toBeInTheDocument();
  });

  test("the scroll region is reachable by keyboard and carries a name", () => {
    // `overflow-x-auto` makes this a scroll container; one that only a pointer
    // can reach fails WCAG 2.1.1, and an unnamed stop in the tab order tells a
    // screen-reader user nothing about what they have landed on.
    render(<CopyableSnippet label="Webhook configuration" snippet={SNIPPET} />);

    const region = screen.getByRole("group", { name: "Webhook configuration" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveTextContent(SNIPPET);
  });

  test("renders the caller's explanation between the label and the snippet", () => {
    render(
      <CopyableSnippet label="Webhook configuration" snippet={SNIPPET}>
        <p>Add this to slskd&rsquo;s config.</p>
      </CopyableSnippet>,
    );

    expect(screen.getByText("Add this to slskd’s config.")).toBeInTheDocument();
  });
});
