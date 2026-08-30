import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { SlskdPanel } from "@/components/settings/SlskdPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const SETTINGS = `${window.location.origin}/api/slskd/settings`;
const TEST_URL = `${window.location.origin}/api/slskd/test`;

function settings(overrides: Record<string, unknown> = {}) {
  return {
    base_url: "",
    downloads_prefix: "",
    auto_import: false,
    has_token: false,
    has_webhook_secret: false,
    ...overrides,
  };
}

describe("SlskdPanel", () => {
  // The panel is settings-only now (the set-aside/review surface moved to the
  // Review page), so the only request it makes is GET /api/slskd/settings —
  // each test registers it.

  test("renders a real h2 heading (not a CardTitle div)", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);
    expect(await screen.findByRole("heading", { level: 2, name: "slskd" })).toBeInTheDocument();
  });

  test("shows the webhook config through the shared snippet block", async () => {
    // The second call site of CopyableSnippet. It used to be a verbatim copy
    // of the sign-in page's block — same handler, same timeout, same markup —
    // which is how the keyboard-reachability fix could land on one and miss
    // the other.
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    const region = await screen.findByRole("group", {
      name: "Webhook configuration",
    });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveTextContent("DownloadDirectoryComplete");
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
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    await screen.findByLabelText(/base url/i);
    expect(screen.getByText(/api\/slskd\/webhook/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
  });

  test("points to the Review page for the set-aside backlog (no inline activity)", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    const link = await screen.findByRole("link", { name: /review/i });
    expect(link).toHaveAttribute("href", "/review");
  });
});
