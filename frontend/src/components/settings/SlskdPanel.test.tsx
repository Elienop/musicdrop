import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { SlskdPanel } from "@/components/settings/SlskdPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

/** Captured before any test stubs it — restoring ONLY navigator, because
 * `vi.unstubAllGlobals()` would also drop test/setup.ts's own stubs. */
const REAL_NAVIGATOR = globalThis.navigator;

afterEach(() => {
  vi.stubGlobal("navigator", REAL_NAVIGATOR);
});

/**
 * A SECURE context, which is what a test asserting the Copy button has to be
 * in: `CopyableSnippet` now feature-detects `navigator.clipboard` and renders
 * a "select the text by hand" line instead of a dead button where it is
 * missing — and jsdom's navigator has no clipboard, exactly like the plain-http
 * LAN origin the README calls MusicDrop's primary deployment. Without this the
 * two assertions below were asking for a button that origin never shows.
 * The insecure case is covered where it belongs, in CopyableSnippet's own test.
 */
function stubSecureContext() {
  vi.stubGlobal("navigator", {
    ...navigator,
    clipboard: { writeText: async () => {} },
  });
}

const SETTINGS = `${window.location.origin}/api/slskd/settings`;
const TEST_URL = `${window.location.origin}/api/slskd/test`;
const IMPORT_OP_URL = `${window.location.origin}/api/config/import-operation`;
/** The auto-import switch's help, as the switch reads it. */
const AUTO_IMPORT_HELP =
  "When on, a finished slskd download imports itself into the library; uncertain matches are set aside for review.";
/** Path in slskd's help, named by slskd.yml's own key. */
const HELP = "slskd’s download folder (directories.downloads), as slskd sees it.";

function settings(overrides: Record<string, unknown> = {}) {
  return {
    base_url: "",
    downloads_prefix: "",
    auto_import: false,
    has_token: false,
    has_webhook_secret: false,
    last_download_missed: false,
    ...overrides,
  };
}

// The card's files line reads the operation beets loaded: `move` unless a
// test registers its own.
beforeEach(() => {
  server.use(
    http.get(IMPORT_OP_URL, () => HttpResponse.json({ operation: "move" })),
  );
});

describe("SlskdPanel", () => {
  // The panel is settings-only now (the set-aside/review surface moved to the
  // Review page). It reads GET /api/slskd/settings, which each test registers,
  // and the import operation, which the beforeEach above answers.

  test("renders a real h2 heading (not a CardTitle div)", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);
    expect(await screen.findByRole("heading", { level: 2, name: "slskd" })).toBeInTheDocument();
  });

  test("shows the webhook config through the shared snippet block", async () => {
    // The second call site of CopyableSnippet. It used to be a verbatim copy
    // of the sign-in page's block — same handler, same timeout, same markup —
    // which is how a fix could land on one and miss the other.
    stubSecureContext();
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    // The block wraps rather than scrolling, so it carries no tab stop. This
    // is the caller that made that call worth it: the webhook `url:` line is
    // 126 chars, and scrolling hid two thirds of it — including the trailing
    // `# host must be an IP…` comment, which is the part that makes it work.
    const region = await screen.findByText(/DownloadDirectoryComplete/);
    expect(region.tagName).toBe("PRE");
    expect(region).not.toHaveAttribute("tabindex");
    expect(region).toHaveTextContent("MUSICDROP_ALLOWED_HOSTS");
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });

  test("saves settings, omitting blank secrets, and confirms the save", async () => {
    let body: Record<string, unknown> | null = null;
    let saved = false;
    server.use(
      // The GET reflects the saved value once the PUT lands, matching a real
      // backend round-trip (so the editor reseeds and confirms, not strands).
      http.get(SETTINGS, () =>
        HttpResponse.json(
          saved
            ? settings({ base_url: "http://slskd:9999", has_token: true })
            : settings({ base_url: "http://slskd:5030", has_token: true }),
        ),
      ),
      http.put(SETTINGS, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        saved = true;
        return HttpResponse.json(settings({ base_url: "http://slskd:9999", has_token: true }));
      }),
    );
    renderWithProviders(<SlskdPanel />);

    const input = await screen.findByLabelText(/base url/i);
    await userEvent.clear(input);
    await userEvent.type(input, "http://slskd:9999");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.base_url).toBe("http://slskd:9999");
    // Both secret fields were left blank, so the PUT omits them (keeps the saved
    // ones); auto_import is always sent.
    expect("token" in body!).toBe(false);
    expect("webhook_secret" in body!).toBe(false);
    expect(body!.auto_import).toBe(false);
    // A polite confirmation appears once the reseeded snapshot is clean again.
    expect(await screen.findByText(/slskd settings saved/i)).toBeInTheDocument();
  });

  test("the auto-import toggle is sent in the PUT body", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.put(SETTINGS, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(settings({ auto_import: true }));
      }),
    );
    renderWithProviders(<SlskdPanel />);

    const toggle = await screen.findByRole("switch", { name: /auto-import/i });
    await userEvent.click(toggle);
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.auto_import).toBe(true);
  });

  test("disables Test connection while the form is dirty (Test uses saved settings)", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ base_url: "http://slskd:5030" }))),
    );
    renderWithProviders(<SlskdPanel />);

    const input = await screen.findByLabelText(/base url/i);
    const testBtn = screen.getByRole("button", { name: /test connection/i });
    expect(testBtn).toBeEnabled();
    await userEvent.type(input, "9");
    expect(testBtn).toBeDisabled();
    expect(screen.getByText(/save before testing/i)).toBeInTheDocument();
  });

  test("tests the connection and shows the reported version", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.post(TEST_URL, () => HttpResponse.json({ ok: true, version: "0.22.3", error: null })),
    );
    renderWithProviders(<SlskdPanel />);

    await screen.findByLabelText(/base url/i);
    await userEvent.click(screen.getByRole("button", { name: /test connection/i }));

    expect(await screen.findByText(/0\.22\.3/)).toBeInTheDocument();
  });

  test("a later save failure clears the stale success message", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ base_url: "http://slskd:5030" }))),
      http.post(TEST_URL, () => HttpResponse.json({ ok: true, version: "0.22.3", error: null })),
      http.put(SETTINGS, () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<SlskdPanel />);

    const input = await screen.findByLabelText(/base url/i);
    // A successful test shows the green "Connected to slskd 0.22.3." status.
    await userEvent.click(screen.getByRole("button", { name: /test connection/i }));
    expect(await screen.findByText(/0\.22\.3/)).toBeInTheDocument();

    // Editing and saving into a failure must clear that stale success.
    await userEvent.type(input, "9");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(
      await screen.findByText(/couldn.t save slskd settings/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/0\.22\.3/)).not.toBeInTheDocument();
  });

  test("shows the paste-in webhook snippet", async () => {
    stubSecureContext();
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    await screen.findByLabelText(/base url/i);
    expect(screen.getByText(/api\/slskd\/webhook/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
  });

  test("labels the prefix field Path in slskd, with its placeholder and help", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    const field = await screen.findByLabelText("Path in slskd");
    // Empty is usually right (slskd sees the same path), so the placeholder
    // names that case instead of showing a path that reads as one to copy.
    expect(field).toHaveAttribute("placeholder", "Same as Folder");
    // The help is split by a <code> around the key name, so match the paragraph.
    expect(
      screen.getByText((_, el) => el?.tagName === "P" && el.textContent === HELP),
    ).toBeInTheDocument();
    expect(field).toHaveAccessibleDescription(HELP);
    // The secret's help is split by a <code>, so match the whole paragraph.
    expect(
      screen.getByText(
        (_, el) =>
          el?.tagName === "P" &&
          el.textContent === "The secret slskd sends as X-API-Key with each webhook.",
      ),
    ).toBeInTheDocument();
  });

  test("the webhook snippet sets slskd's own retry beside call:", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    const block = await screen.findByText(/DownloadDirectoryComplete/);
    const text = block.textContent ?? "";
    // `retry` is a sibling of `call` (6 spaces), after the headers, with
    // `attempts` under it (8), spelled as slskd spells it. Nested under
    // `call:` instead, slskd would not read it and would try only once.
    expect(text).toContain("\n      call:\n");
    expect(text).toContain(
      "            value: <your webhook secret>\n      retry:\n        attempts: 10",
    );
  });

  test("says when the last download didn't match Path in slskd", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ last_download_missed: true }))),
    );
    renderWithProviders(<SlskdPanel />);

    const line = await screen.findByText("Last download didn’t match Path in slskd.");
    // A remembered state, not an event: an alert role would be announced on
    // every visit to Settings.
    expect(line.closest("p")).not.toHaveAttribute("role");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // The field it is about is described by its help AND the miss line.
    expect(screen.getByLabelText("Path in slskd")).toHaveAccessibleDescription(
      `${HELP} Last download didn’t match Path in slskd.`,
    );
  });

  test("shows no miss line when the last download matched", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    // Control: the editor has rendered, so an absent line is not a load race.
    const field = await screen.findByLabelText("Path in slskd");
    expect(screen.queryByText(/didn.t match Path in slskd/)).not.toBeInTheDocument();
    // Only the help describes the field while the line is absent, and no id
    // it names dangles (a missing id is dropped silently from the description).
    expect(field).toHaveAccessibleDescription(HELP);
    const ids = field.getAttribute("aria-describedby")?.split(" ") ?? [];
    expect(ids.filter((id) => document.getElementById(id) === null)).toEqual([]);
  });

  test("points to the Review page for the set-aside backlog (no inline activity)", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    const link = await screen.findByRole("link", { name: /review/i });
    expect(link).toHaveAttribute("href", "/review");
  });
});

describe("SlskdPanel: what an import does with the files", () => {
  const autoImport = () =>
    screen.getByRole("switch", { name: "Auto-import completed downloads" });

  test.each<[string, string]>([
    ["move", "Files move into your library."],
    ["hardlink", "Files stay, hardlinked into your library."],
    ["copy", "Files stay, copied into your library."],
    ["link", "Files stay, symlinked into your library."],
    ["reflink", "Files stay, cloned into your library."],
    ["reflink_auto", "Files stay, cloned into your library."],
    ["in_place", "Files stay where they are."],
  ])("%s: the line under auto-import, read with it", async (op, line) => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(IMPORT_OP_URL, () => HttpResponse.json({ operation: op })),
    );
    renderWithProviders(<SlskdPanel />);
    const change = await screen.findByRole("link", { name: "Change" });
    expect(change).toHaveAttribute("href", "/settings/beets");
    expect(change.closest("p")?.textContent).toBe(`${line} Change`);
    // The switch is described by its help and the sentence, not the link's word.
    expect(autoImport()).toHaveAccessibleDescription(
      `${AUTO_IMPORT_HELP} ${line}`,
    );
  });

  test("no line while the setting loads", async () => {
    let reads = 0;
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(IMPORT_OP_URL, () => {
        reads += 1;
        return new Promise<Response>(() => {});
      }),
    );
    renderWithProviders(<SlskdPanel />);
    await screen.findByLabelText("Path in slskd");
    await waitFor(() => expect(reads).toBe(1));
    expect(screen.queryByRole("link", { name: "Change" })).toBeNull();
    expect(screen.queryByText(/^Files /)).toBeNull();
    expect(autoImport()).toHaveAccessibleDescription(AUTO_IMPORT_HELP);
  });

  test("no line when the setting can't be read", async () => {
    let reads = 0;
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(IMPORT_OP_URL, () => {
        reads += 1;
        return HttpResponse.json({ detail: "boom" }, { status: 500 });
      }),
    );
    renderWithProviders(<SlskdPanel />);
    await screen.findByLabelText("Path in slskd");
    await waitFor(() => expect(reads).toBeGreaterThan(0));
    // Let the failed read settle before looking for the line.
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("link", { name: "Change" })).toBeNull();
    expect(screen.queryByText(/^Files /)).toBeNull();
    expect(autoImport()).toHaveAccessibleDescription(AUTO_IMPORT_HELP);
  });
});
