import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, test } from "vitest";

import { PlexSettingsPanel } from "@/components/settings/PlexSettingsPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const SETTINGS = `${window.location.origin}/api/plex/settings`;
const TEST_URL = `${window.location.origin}/api/plex/test`;
const SECTIONS = `${window.location.origin}/api/plex/sections`;

function settings(overrides: Record<string, unknown> = {}) {
  return { base_url: "", library_path: "", library_section: "", has_token: false, ...overrides };
}

describe("PlexSettingsPanel", () => {
  // The editor probes Plex library sections on mount for the section dropdown.
  // Default to an empty list so tests that don't care never hit an unhandled
  // request; the section tests register their own /api/plex/sections handler.
  beforeEach(() => {
    server.use(http.get(SECTIONS, () => HttpResponse.json({ sections: [] })));
  });

  test("renders a real h2 heading (not a CardTitle div)", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<PlexSettingsPanel />);
    expect(await screen.findByRole("heading", { level: 2, name: "Plex" })).toBeInTheDocument();
  });

  test("saves settings, omitting a blank token, and confirms the save", async () => {
    let body: Record<string, unknown> | null = null;
    let saved = false;
    server.use(
      // The GET reflects the saved value once the PUT lands, matching a real
      // backend round-trip (so the editor reseeds and confirms, not strands).
      http.get(SETTINGS, () =>
        HttpResponse.json(
          saved
            ? settings({ base_url: "http://plex:9999", has_token: true })
            : settings({ base_url: "http://plex:32400", has_token: true }),
        ),
      ),
      http.put(SETTINGS, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        saved = true;
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
    // A polite confirmation appears once the reseeded snapshot is clean again.
    expect(await screen.findByText(/plex settings saved/i)).toBeInTheDocument();
  });

  test("disables Test connection while the form is dirty (Test uses saved settings)", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ base_url: "http://plex:32400" }))),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const input = await screen.findByLabelText(/base url/i);
    const testBtn = screen.getByRole("button", { name: /test connection/i });
    expect(testBtn).toBeEnabled();
    await userEvent.type(input, "9");
    expect(testBtn).toBeDisabled();
    expect(screen.getByText(/save before testing/i)).toBeInTheDocument();
  });

  test("offers the music sections and saves library_section", async () => {
    let body: Record<string, unknown> | null = null;
    let saved = false;
    server.use(
      http.get(SETTINGS, () =>
        HttpResponse.json(saved ? settings({ library_section: "MusicDrop" }) : settings()),
      ),
      http.get(SECTIONS, () => HttpResponse.json({ sections: ["Music", "MusicDrop"] })),
      http.put(SETTINGS, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        saved = true;
        return HttpResponse.json(settings({ library_section: "MusicDrop" }));
      }),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const select = await screen.findByLabelText(/library section/i);
    // The server's music sections are offered as options (alongside the
    // always-present "Auto — first music library" empty option).
    expect(
      await within(select).findByRole("option", { name: /musicdrop/i }),
    ).toBeInTheDocument();
    await userEvent.selectOptions(select, "MusicDrop");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.library_section).toBe("MusicDrop");
  });

  test("keeps the saved section selectable when the sections fetch fails", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ library_section: "Vinyl" }))),
      // The section probe fails (e.g. Plex unreachable) — the current value must
      // still be present and selected so a failed fetch never hides it.
      http.get(SECTIONS, () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const select = await screen.findByLabelText(/library section/i);
    expect(within(select).getByRole("option", { name: "Vinyl" })).toBeInTheDocument();
    expect(select).toHaveValue("Vinyl");
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

  test("a later save failure clears the stale success message", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ base_url: "http://plex:32400" }))),
      http.post(TEST_URL, () =>
        HttpResponse.json({ ok: true, server_name: "Living Room", error: null }),
      ),
      http.put(SETTINGS, () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const input = await screen.findByLabelText(/base url/i);
    // A successful test shows the green "Connected to Living Room." status.
    await userEvent.click(screen.getByRole("button", { name: /test connection/i }));
    expect(await screen.findByText(/living room/i)).toBeInTheDocument();

    // Editing and saving into a failure must clear that stale success — the UI
    // must not show a green "connected" line next to a red "couldn't save".
    await userEvent.type(input, "9");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(
      await screen.findByText(/couldn.t save plex settings/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/living room/i)).not.toBeInTheDocument();
  });

  test("a later successful test clears a stale save-failure alert", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ base_url: "http://plex:32400" }))),
      http.put(SETTINGS, () => new HttpResponse(null, { status: 500 })),
      http.post(TEST_URL, () =>
        HttpResponse.json({ ok: true, server_name: "Living Room", error: null }),
      ),
    );
    renderWithProviders(<PlexSettingsPanel />);

    // Save the (clean) form into a failure — the red "couldn't save" alert shows.
    await screen.findByLabelText(/base url/i);
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(
      await screen.findByText(/couldn.t save plex settings/i),
    ).toBeInTheDocument();

    // A subsequent successful test must clear the stale failure alert.
    await userEvent.click(screen.getByRole("button", { name: /test connection/i }));
    expect(await screen.findByText(/living room/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/couldn.t save plex settings/i),
    ).not.toBeInTheDocument();
  });
});
