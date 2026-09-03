import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

vi.mock("@/pages/settings/NamingPanel", () => ({
  NamingPanel: () => <div data-testid="naming-panel" />,
}));
vi.mock("@/pages/settings/LyricsBackfillPanel", () => ({
  LyricsBackfillPanel: () => <div data-testid="lyrics-panel" />,
}));
vi.mock("@/pages/settings/ArtistImagesPanel", () => ({
  ArtistImagesPanel: () => <div data-testid="artist-images-panel" />,
}));
vi.mock("@/pages/settings/ArtistArtPanel", () => ({
  ArtistArtPanel: () => <div data-testid="artist-art-panel" />,
}));
vi.mock("@/components/settings/PlexSettingsPanel", () => ({
  PlexSettingsPanel: () => <div data-testid="plex-panel" />,
}));
vi.mock("@/components/settings/SlskdPanel", () => ({
  SlskdPanel: () => <div data-testid="slskd-panel" />,
}));
vi.mock("@/pages/settings/AccountPanel", () => ({
  AccountPanel: () => <div data-testid="account-panel" />,
}));

import { SettingsAccountPage } from "@/pages/settings/SettingsAccountPage";
import { SettingsIntegrationsPage } from "@/pages/settings/SettingsIntegrationsPage";
import { SettingsMetadataPage } from "@/pages/settings/SettingsMetadataPage";
import { SettingsNamingPage } from "@/pages/settings/SettingsNamingPage";

describe("settings section pages", () => {
  test("naming hosts the NamingPanel", () => {
    render(<SettingsNamingPage />);
    expect(screen.getByTestId("naming-panel")).toBeInTheDocument();
  });

  test("metadata hosts lyrics + artist images + artist art", () => {
    render(<SettingsMetadataPage />);
    expect(screen.getByTestId("lyrics-panel")).toBeInTheDocument();
    expect(screen.getByTestId("artist-images-panel")).toBeInTheDocument();
    expect(screen.getByTestId("artist-art-panel")).toBeInTheDocument();
  });

  test("integrations hosts plex + slskd", () => {
    render(<SettingsIntegrationsPage />);
    expect(screen.getByTestId("plex-panel")).toBeInTheDocument();
    expect(screen.getByTestId("slskd-panel")).toBeInTheDocument();
  });

  test("account hosts the AccountPanel", () => {
    render(<SettingsAccountPage />);
    expect(screen.getByTestId("account-panel")).toBeInTheDocument();
  });
});
