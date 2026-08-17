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

// Sections as /api/plex/sections reports them: a title plus the folder paths
// Plex holds for that library.
const SECTION_MUSIC = { title: "Music", locations: ["/data/music"] };
const SECTION_DROP = { title: "MusicDrop", locations: ["/musicdrop"] };
const SECTION_SPLIT = { title: "Split", locations: ["/musicdrop", "/mnt/spill"] };

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
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_MUSIC, SECTION_DROP] })),
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
      // A configured path is essential here: with a blank one the mismatch
      // branch can't fire anyway, so the "no scare" assertion below would hold
      // for an implementation that warns off zero known folders.
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "Vinyl", library_path: "/music" })),
      ),
      // The section probe fails (e.g. Plex unreachable) — the current value must
      // still be present and selected so a failed fetch never hides it.
      http.get(SECTIONS, () => new HttpResponse(null, { status: 500 })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const select = await screen.findByLabelText(/library section/i);
    expect(within(select).getByRole("option", { name: "Vinyl" })).toBeInTheDocument();
    expect(select).toHaveValue("Vinyl");
    // Nothing is known about the section's folders, so the panel claims nothing:
    // an unreachable Plex must not produce a mismatch scare. `findBy` + rejects,
    // not `queryBy`: a failed probe has no positive signal to wait on, so a bare
    // queryBy would pass simply by running before the fetch settled.
    await expect(screen.findByText(/plex reports/i)).rejects.toThrow();
    await expect(screen.findByText(/doesn.t list this path/i)).rejects.toThrow();
  });

  test("shows the folders Plex reports for the selected library", async () => {
    server.use(
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "MusicDrop", library_path: "/musicdrop" })),
      ),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_MUSIC, SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    // The folder Plex holds for the SELECTED library, not for the other one.
    expect(await screen.findByText("/musicdrop")).toBeInTheDocument();
    expect(screen.queryByText("/data/music")).not.toBeInTheDocument();
    // The configured path agrees with it, so no warning.
    expect(screen.queryByText(/doesn.t list this path/i)).not.toBeInTheDocument();
  });

  test("fills the library path in from the section you pick", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_MUSIC, SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const path = await screen.findByLabelText(/library path/i);
    expect(path).toHaveValue("");
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /musicdrop/i });
    await userEvent.selectOptions(select, "MusicDrop");

    // The whole point: the user never retypes a path the app just read off Plex.
    expect(path).toHaveValue("/musicdrop");
  });

  test("replaces a path inherited from the previously picked section", async () => {
    server.use(
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "Music", library_path: "/data/music" })),
      ),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_MUSIC, SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const path = await screen.findByLabelText(/library path/i);
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /musicdrop/i });
    await userEvent.selectOptions(select, "MusicDrop");

    // "/data/music" was the OLD section's folder, so it is a leftover of that
    // pick, not a deliberate value — leaving it would be the original bug.
    expect(path).toHaveValue("/musicdrop");
  });

  test("keeps a hand-typed path when you pick a section, and warns instead", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_MUSIC, SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const path = await screen.findByLabelText(/library path/i);
    await userEvent.type(path, "/mnt/odd-mount");
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /musicdrop/i });
    await userEvent.selectOptions(select, "MusicDrop");

    expect(path).toHaveValue("/mnt/odd-mount");
    // Warned, not blocked — and the warning names the consequence, because
    // "path matching silently fails" is exactly what nobody notices.
    expect(await screen.findByText(/doesn.t list this path/i)).toBeInTheDocument();
    expect(screen.getByText(/artist and title/i)).toBeInTheDocument();
  });

  test("warns about a mismatch the saved settings already carry", async () => {
    server.use(
      // The real incident: library_path "/music" against a library that lives
      // at "/musicdrop". Every path lookup missed, for every sync ever run.
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "MusicDrop", library_path: "/music" })),
      ),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_MUSIC, SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    expect(await screen.findByText(/doesn.t list this path/i)).toBeInTheDocument();
    // Warning only: the typed value stands and Save is never disabled.
    expect(await screen.findByLabelText(/library path/i)).toHaveValue("/music");
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });

  test("treats a trailing slash as the same folder, not a mismatch", async () => {
    server.use(
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "MusicDrop", library_path: "/musicdrop/" })),
      ),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    await screen.findByText("/musicdrop");
    expect(screen.queryByText(/doesn.t list this path/i)).not.toBeInTheDocument();
  });

  test("offers every folder and fills nothing when a library spans several", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_SPLIT] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const path = await screen.findByLabelText(/library path/i);
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /split/i });
    await userEvent.selectOptions(select, "Split");

    // Both folders are shown; neither is guessed at, because only the user
    // knows which one their beets root maps onto.
    expect(await screen.findByText("/musicdrop")).toBeInTheDocument();
    expect(screen.getByText("/mnt/spill")).toBeInTheDocument();
    expect(path).toHaveValue("");
  });

  test("explains what a blank path claims instead of calling it a mismatch", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ library_section: "MusicDrop" }))),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    // Blank is a claim ("we share a mount") that may well be true, and the panel
    // cannot check it — so it is explained, not flagged like a wrong path.
    expect(await screen.findByText(/passes paths through unchanged/i)).toBeInTheDocument();
    expect(screen.queryByText(/doesn.t list this path/i)).not.toBeInTheDocument();
  });

  test("resolves Auto to the only music library, as the backend does", async () => {
    server.use(
      // library_section blank = "Auto: first music library". The backend picks
      // the sole artist section (and refuses when there are several), so with
      // exactly one the panel can name its folders honestly.
      http.get(SETTINGS, () => HttpResponse.json(settings({ library_path: "/music" }))),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    expect(await screen.findByText("/musicdrop")).toBeInTheDocument();
    expect(screen.getByText(/doesn.t list this path/i)).toBeInTheDocument();
  });

  test("claims nothing about Auto when the server has several music libraries", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ library_path: "/music" }))),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_MUSIC, SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    // Wait for the sections to actually be in play — an option only exists once
    // the probe resolved — or the assertions below would pass on a panel that
    // simply hadn't fetched yet.
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /musicdrop/i });
    // The backend REFUSES to guess between two music libraries, so the panel
    // must not name one either.
    await expect(screen.findByText(/plex reports/i)).rejects.toThrow();
    await expect(screen.findByText(/doesn.t list this path/i)).rejects.toThrow();
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
