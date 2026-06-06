import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { PlexSettingsPanel } from "@/components/settings/PlexSettingsPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const SETTINGS = `${window.location.origin}/api/plex/settings`;
const TEST_URL = `${window.location.origin}/api/plex/test`;

function settings(overrides: Record<string, unknown> = {}) {
  return { base_url: "", library_path: "", has_token: false, ...overrides };
}

describe("PlexSettingsPanel", () => {
  test("saves settings, omitting a blank token so the saved one is kept", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ base_url: "http://plex:32400", has_token: true })),
      ),
      http.put(SETTINGS, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(settings({ base_url: "http://plex:9999", has_token: true }));
      }),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const input = await screen.findByLabelText(/base url/i);
    await userEvent.clear(input);
    await userEvent.type(input, "http://plex:9999");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.base_url).toBe("http://plex:9999");
    // The token field was left blank, so the PUT omits it (keeps the saved one).
    expect("token" in body!).toBe(false);
  });

  test("tests the connection and shows the server name", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.post(TEST_URL, () =>
        HttpResponse.json({ ok: true, server_name: "Living Room", error: null }),
      ),
    );
    renderWithProviders(<PlexSettingsPanel />);

    await screen.findByLabelText(/base url/i);
    await userEvent.click(screen.getByRole("button", { name: /test connection/i }));

    expect(await screen.findByText(/living room/i)).toBeInTheDocument();
  });
});
