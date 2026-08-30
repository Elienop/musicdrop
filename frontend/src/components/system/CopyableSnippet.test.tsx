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

/** A navigator with NO clipboard at all — what every browser hands an insecure
 * origin, because `navigator.clipboard` is gated on a secure context. Built by
 * DELETING the property rather than by stubbing a rejecting `writeText`: in a
 * real insecure context `writeText` is never reached, so a rejecting stub
 * models a code path that origin cannot take. */
function stubInsecureContext() {
  // Typed as a mutable record, not `Partial<Navigator>`: `Navigator.clipboard`
  // is declared readonly, and `delete` on a readonly property does not compile.
  // The `delete` stays because it is the assertion — whatever the spread of a
  // host object did or did not carry over, this navigator has no clipboard.
  const nav: Record<string, unknown> = { ...navigator };
  delete nav.clipboard;
  vi.stubGlobal("navigator", nav);
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

  test("offers no Copy button on an insecure origin, and says what to do instead", async () => {
    // MusicDrop's PRIMARY deployment, per the README: plain http on a LAN
    // address like http://192.168.1.10:3030. That is not a secure context
    // (http://localhost is), so `navigator.clipboard` is `undefined`,
    // `navigator.clipboard.writeText(...)` threw a TypeError, and the
    // handler's catch swallowed it — a button that did nothing at all, on the
    // first screen a new operator sees, for the one action the copy asks them
    // to take.
    stubInsecureContext();
    render(<CopyableSnippet label="Generate a password hash" snippet={SNIPPET} />);

    expect(screen.queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
    expect(
      screen.getByText(/copying needs a secure page/i),
    ).toBeInTheDocument();
    // The fallback the message points at has to actually be there: the block
    // wraps, so every character is selectable by hand.
    expect(screen.getByText(SNIPPET)).toBeInTheDocument();
  });

  test("a clipboard that refuses the write says so, instead of nothing", async () => {
    // The OTHER failure, and the one that survives feature detection: the API
    // is present (secure context) and rejects anyway — a denied permission, an
    // unfocused document. Silence here was the same dead-button experience.
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    stubClipboard(writeText);
    render(<CopyableSnippet label="Generate a password hash" snippet={SNIPPET} />);

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(
      await screen.findByText(/this browser refused the copy/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copied" })).not.toBeInTheDocument();
    expect(screen.getByText(SNIPPET)).toBeInTheDocument();
  });

  test("a working clipboard shows neither fallback message", async () => {
    // The negative control. Without it both messages could be permanently
    // rendered and every assertion above would still pass.
    stubClipboard(vi.fn().mockResolvedValue(undefined));
    render(<CopyableSnippet label="Generate a password hash" snippet={SNIPPET} />);

    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
    expect(screen.queryByText(/copying needs a secure page/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/refused the copy/i)).not.toBeInTheDocument();
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
