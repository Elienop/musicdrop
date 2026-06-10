import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SettingsSection } from "@/components/system/SettingsSection";

describe("SettingsSection", () => {
  it("renders the title as a level-2 heading at the section scale", () => {
    render(
      <SettingsSection title="Plex">
        <span>form</span>
      </SettingsSection>,
    );
    // The demotion: settings panels are sections (text-base h2), not page
    // titles — the route's single h1 belongs to PageHeader.
    const h2 = screen.getByRole("heading", { level: 2, name: "Plex" });
    expect(h2).toHaveClass("text-base", "font-semibold");
  });

  it("puts the heading INSIDE the bordered panel shell", () => {
    render(
      <SettingsSection title="Plex">
        <span>form</span>
      </SettingsSection>,
    );
    const h2 = screen.getByRole("heading", { level: 2, name: "Plex" });
    const shell = h2.closest("section");
    expect(shell).not.toBeNull();
    expect(shell).toHaveClass("rounded-xl", "border", "p-4");
  });

  it("names the landmark after the title", () => {
    render(
      <SettingsSection title="Plex">
        <span>form</span>
      </SettingsSection>,
    );
    expect(screen.getByRole("region", { name: "Plex" })).toBeInTheDocument();
  });

  it("renders the description when given", () => {
    render(
      <SettingsSection title="Plex" description="Connect your Plex server.">
        <span>form</span>
      </SettingsSection>,
    );
    expect(
      screen.getByText("Connect your Plex server."),
    ).toBeInTheDocument();
  });

  it("omits the description line when absent", () => {
    const { container } = render(
      <SettingsSection title="Plex">
        <span>form</span>
      </SettingsSection>,
    );
    expect(container.querySelector("p")).toBeNull();
  });

  it("renders children inside the shell", () => {
    render(
      <SettingsSection title="Plex">
        <span>form</span>
      </SettingsSection>,
    );
    expect(screen.getByText("form")).toBeInTheDocument();
  });
});
