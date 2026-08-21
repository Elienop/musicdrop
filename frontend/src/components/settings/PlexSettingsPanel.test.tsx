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
// A library whose folder is an ANCESTOR of the path MusicDrop's own root maps
// onto — Plex indexes "/data" and the music actually lives in "/data/music".
const SECTION_SHARED = { title: "Shared", locations: ["/data"] };

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
    // always-present "Auto" empty option).
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

  test("leaves a blank path blank when you pick a section, and explains why", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_SHARED] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const path = await screen.findByLabelText(/library path/i);
    expect(path).toHaveValue("");
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /shared/i });
    await userEvent.selectOptions(select, "Shared");

    // Blank is not an empty slot to fill helpfully — it is the setting that
    // means "Plex sees the same paths I do", and it is CORRECT when the two
    // share a mount. Filling "/data" over a shared root of "/data/music" would
    // make translate_path rebase every track one level too high and every
    // lookup would miss. The panel cannot see MusicDrop's own root, so it must
    // not decide.
    expect(path).toHaveValue("");
    // Silently doing nothing where the user expects help is its own failure:
    // the hint has to own the choice and say what to type instead.
    const blankHint = await screen.findByText(/passes paths through unchanged/i);
    expect(blankHint).toHaveTextContent(/nothing is filled in for you/i);
    expect(screen.queryByText(/doesn.t list this path/i)).not.toBeInTheDocument();
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

  test("keeps replacing its OWN fill as you try one library after another", async () => {
    server.use(
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "Music", library_path: "/data/music" })),
      ),
      http.get(SECTIONS, () =>
        HttpResponse.json({ sections: [SECTION_MUSIC, SECTION_DROP, SECTION_SHARED] }),
      ),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const path = await screen.findByLabelText(/library path/i);
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /musicdrop/i });
    await userEvent.selectOptions(select, "MusicDrop");
    expect(path).toHaveValue("/musicdrop");

    // "/musicdrop" is now the panel's own fill, so it is still the panel's to
    // replace: shopping through libraries must not strand you on the folder of
    // whichever one you happened to try second.
    await userEvent.selectOptions(select, "Shared");
    expect(path).toHaveValue("/data");
  });

  test("still replaces its own fill after a library it could not fill", async () => {
    server.use(
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "Music", library_path: "/data/music" })),
      ),
      http.get(SECTIONS, () =>
        HttpResponse.json({
          sections: [SECTION_MUSIC, SECTION_SPLIT, SECTION_SHARED, SECTION_DROP],
        }),
      ),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const path = await screen.findByLabelText(/library path/i);
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /shared/i });
    await userEvent.selectOptions(select, "Shared");
    expect(path).toHaveValue("/data");

    // Split spans two folders, so it fills nothing and leaves "/data" standing.
    await userEvent.selectOptions(select, "Split");
    expect(path).toHaveValue("/data");

    // "/data" is STILL the panel's own fill, never something the user touched.
    // Recognising it only as "a folder of the library selected a moment ago"
    // loses it here — the library selected a moment ago is Split.
    await userEvent.selectOptions(select, "MusicDrop");
    expect(path).toHaveValue("/musicdrop");
  });

  test("keeps a hand-typed folder of a multi-folder library when you pick another section", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ library_section: "Split" }))),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_SPLIT, SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    // Split spans two folders and the panel fills nothing for it, so typing the
    // one your beets root maps onto IS how this gets configured.
    const path = await screen.findByLabelText(/library path/i);
    await userEvent.type(path, "/mnt/spill");
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /musicdrop/i });
    await userEvent.selectOptions(select, "MusicDrop");

    // Reading "equals a folder of the library selected a moment ago" as
    // leftover spends this on MusicDrop's "/musicdrop" — a value the user never
    // typed, and one no pick can bring back, since Split fills nothing.
    expect(path).toHaveValue("/mnt/spill");
  });

  test("keeps a hand-typed path even when it is exactly the folder Plex reports", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ library_section: "MusicDrop" }))),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_DROP, SECTION_MUSIC] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    // Reading the folder list under the field and typing what it says is the
    // obvious thing to do — and it makes the typed value indistinguishable from
    // a fill by VALUE. Only who wrote it tells them apart.
    const path = await screen.findByLabelText(/library path/i);
    await userEvent.type(path, "/musicdrop");
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /^music$/i });
    await userEvent.selectOptions(select, "Music");

    // Changing library does not license overwriting what the user typed: the
    // path now disagrees with the new library, and the warning says so.
    expect(path).toHaveValue("/musicdrop");
    expect(await screen.findByText(/doesn.t list this path/i)).toBeInTheDocument();
  });

  test("keeps a SAVED path a multi-folder library reports, which it never filled", async () => {
    server.use(
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "Split", library_path: "/mnt/spill" })),
      ),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_SPLIT, SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const path = await screen.findByLabelText(/library path/i);
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /musicdrop/i });
    await userEvent.selectOptions(select, "MusicDrop");

    // Same value, arriving from the server instead of the keyboard: the panel
    // cannot have authored it (it fills nothing for a multi-folder library), so
    // it is someone's deliberate setting either way.
    expect(path).toHaveValue("/mnt/spill");
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

  test("accepts a path inside a folder Plex reports rather than demanding it exactly", async () => {
    server.use(
      // Plex indexes "/data" recursively, and MusicDrop's root maps onto
      // "/data/music" inside it — a legitimate setup that exact equality
      // slandered as broken.
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "Shared", library_path: "/data/music" })),
      ),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_SHARED] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    expect(await screen.findByText("/data")).toBeInTheDocument();
    expect(screen.queryByText(/doesn.t list this path/i)).not.toBeInTheDocument();
  });

  test("still warns for a sibling folder that merely shares a string prefix", async () => {
    server.use(
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "Music", library_path: "/data/musicians" })),
      ),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_MUSIC] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    // "/data/musicians" starts with the string "/data/music" but is nowhere
    // inside it, so the comparison must be by path SEGMENT.
    expect(await screen.findByText(/doesn.t list this path/i)).toBeInTheDocument();
  });

  test("offers every folder and fills nothing when a library spans several", async () => {
    server.use(
      http.get(SETTINGS, () =>
        HttpResponse.json(settings({ library_section: "Music", library_path: "/data/music" })),
      ),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_MUSIC, SECTION_SPLIT] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const path = await screen.findByLabelText(/library path/i);
    const select = await screen.findByLabelText(/library section/i);
    await within(select).findByRole("option", { name: /split/i });
    await userEvent.selectOptions(select, "Split");

    // "/data/music" is a leftover of the previous pick, so a single-folder
    // library would replace it — but Split spans two, and only the user knows
    // which one their beets root maps onto. Both are shown, neither is guessed.
    expect(await screen.findByText("/musicdrop")).toBeInTheDocument();
    expect(screen.getByText("/mnt/spill")).toBeInTheDocument();
    expect(path).toHaveValue("/data/music");
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
    // Auto is honest on this server, so it must not be warned about.
    expect(screen.queryByText(/multiple plex music libraries found/i)).not.toBeInTheDocument();
  });

  test("describes Auto as the ONLY music library, never the first of several", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<PlexSettingsPanel />);

    // app/plex/client.py, music_section: with a blank title it raises
    // "Multiple Plex music libraries found." when more than one artist section
    // exists — it never takes the first. Copy that promises otherwise sends the
    // user off to meet an unexplained sync failure.
    const select = await screen.findByLabelText(/library section/i);
    const auto = within(select).getByRole("option", { name: /auto/i });
    expect(auto).toHaveTextContent(/only music library/i);
    expect(auto).not.toHaveTextContent(/first/i);
    const hint = screen.getByText(/which plex music library playlists sync into/i);
    expect(hint).toHaveTextContent(/exactly one/i);
    expect(hint).not.toHaveTextContent(/first/i);
  });

  test("claims nothing about Auto's folders when the server has several music libraries", async () => {
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

  test("says Auto can't work when the server has several music libraries", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ library_path: "/music" }))),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_MUSIC, SECTION_DROP] })),
    );
    renderWithProviders(<PlexSettingsPanel />);

    // Going silent about the folders (above) without saying why leaves the user
    // with a dropdown promising Auto works and a green "Test connection" —
    // test_connection never looks at a section — and a sync that raises. Name
    // the failure, in the words the backend will use.
    const warning = await screen.findByText(/multiple plex music libraries found/i);
    expect(warning).toHaveTextContent(/2 music libraries/i);
    expect(warning).toHaveTextContent(/every sync fails/i);
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

  test("saves the library path trimmed, so a padded one can't pass the check and break the join", async () => {
    // Every check in this panel compares the path TRIMMED, so " /musicdrop"
    // matches the reported folder and draws no warning. Sent padded, the server
    // stores it verbatim and translate_path joins it into " /musicdrop/A/x.mp3"
    // — every path lookup misses, silently, on the one setting whose failure
    // this whole panel exists to prevent.
    let sent: Record<string, unknown> | null = null;
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(SECTIONS, () => HttpResponse.json({ sections: [SECTION_DROP] })),
      http.put(SETTINGS, async ({ request }) => {
        sent = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(settings({ library_path: "/musicdrop" }));
      }),
    );
    renderWithProviders(<PlexSettingsPanel />);

    const path = await screen.findByLabelText(/library path/i);
    await userEvent.type(path, "  /musicdrop  ");
    // The panel is satisfied: no mismatch warning, because it compares trimmed.
    expect(screen.queryByText(/doesn.t list this path/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() => expect(sent).not.toBeNull());
    expect(sent!.library_path).toBe("/musicdrop");
  });

  test("saves a whitespace-only library path as blank, which is what it looks like", async () => {
    // Blank means "same mount, pass paths through" to the backend. " " does
    // not: it is a non-empty root that translate_path rebases everything onto.
    let sent: Record<string, unknown> | null = null;
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.put(SETTINGS, async ({ request }) => {
        sent = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(settings());
      }),
    );
    renderWithProviders(<PlexSettingsPanel />);

    await userEvent.type(await screen.findByLabelText(/library path/i), "   ");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() => expect(sent).not.toBeNull());
    expect(sent!.library_path).toBe("");
  });
});
