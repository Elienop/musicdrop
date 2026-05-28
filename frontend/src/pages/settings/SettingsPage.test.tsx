import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { BeetsConfigSnapshot } from "@/api/useBeetsConfig";
import { SettingsPage } from "@/pages/settings/SettingsPage";
import { server } from "@/test/msw-server";
import { renderWithProviders } from "@/test/render";

const CONFIG_URL = `${window.location.origin}/api/config`;

function snapshotFixture(
  overrides: Partial<BeetsConfigSnapshot> = {},
): BeetsConfigSnapshot {
  return {
    yaml_text:
      "directory: /music\nlibrary: library.db\nplugins:\n  - musicbrainz\n  - deezer\n",
    config_path: "/abs/data/beets/config.yaml",
    loaded_at: "2026-05-28T14:23:00Z",
    file_modified_at: "2026-05-28T14:23:00Z",
    restart_required: false,
    ...overrides,
  };
}

describe("SettingsPage", () => {
  test("renders the snapshot YAML and config_path", async () => {
    server.use(http.get(CONFIG_URL, () => HttpResponse.json(snapshotFixture())));
    renderWithProviders(<SettingsPage />, {
      route: "/settings",
      path: "/settings",
    });

    expect(await screen.findByText(/Beets configuration/i)).toBeInTheDocument();
    // Await data arrival before sync-checking the path line (it renders only
    // once `useBeetsConfig` resolves; the title above renders immediately).
    const pre = await screen.findByTestId("config-yaml");
    expect(screen.getByText(/data\/beets\/config\.yaml/)).toBeInTheDocument();
    expect(pre).toHaveTextContent("directory: /music");
    expect(pre).toHaveTextContent("plugins:");
    expect(pre).toHaveTextContent("- deezer");
  });

  test("shows restart banner when restart_required", async () => {
    server.use(
      http.get(CONFIG_URL, () =>
        HttpResponse.json(
          snapshotFixture({
            restart_required: true,
            file_modified_at: "2026-05-28T15:00:00Z",
          }),
        ),
      ),
    );
    renderWithProviders(<SettingsPage />, {
      route: "/settings",
      path: "/settings",
    });
    expect(
      await screen.findByText(/restart MusicDrop to apply/i),
    ).toBeInTheDocument();
  });

  test("hides restart banner when fresh", async () => {
    server.use(
      http.get(CONFIG_URL, () =>
        HttpResponse.json(snapshotFixture({ restart_required: false })),
      ),
    );
    renderWithProviders(<SettingsPage />, {
      route: "/settings",
      path: "/settings",
    });
    await screen.findByText(/Beets configuration/i);
    expect(
      screen.queryByText(/restart MusicDrop to apply/i),
    ).not.toBeInTheDocument();
  });

  test("shows error state when fetch fails", async () => {
    server.use(
      http.get(CONFIG_URL, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderWithProviders(<SettingsPage />, {
      route: "/settings",
      path: "/settings",
    });
    expect(
      await screen.findByText(/could not load configuration/i),
    ).toBeInTheDocument();
  });
});
