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

    expect(
      await screen.findByRole("button", { name: "Copied" }),
    ).toBeInTheDocument();
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

  test("the snippet wraps rather than scrolling, so it needs no tab stop", () => {
    // The whole a11y argument in one assertion set. A scroll container only a
    // pointer can reach fails WCAG 2.1.1, which is why this used to carry
    // tabIndex + role + aria-label. Wrapping removes the scroll container, so
    // all three go — but ONLY while it really does wrap. Both halves are
    // pinned: re-adding `overflow-x-auto` or dropping either wrap utility has
    // to fail here, otherwise the tab stop was deleted for nothing.
    render(<CopyableSnippet label="Webhook configuration" snippet={SNIPPET} />);

    const block = screen.getByText(SNIPPET);
    expect(block.tagName).toBe("PRE");
    expect(block.className).toContain("whitespace-pre-wrap");
    // Guards the long-URL case: without it a single unbreakable token
    // overflows and silently restores the scroll container.
    expect(block.className).toContain("break-words");
    expect(block.className).not.toContain("overflow-x");
    expect(block).not.toHaveAttribute("tabindex");
    expect(block).not.toHaveAttribute("role");
    expect(block).not.toHaveAttribute("aria-label");
    expect(block).toHaveTextContent(SNIPPET);
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
