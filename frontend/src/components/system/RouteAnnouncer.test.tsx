import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { Link, MemoryRouter, Route, Routes } from "react-router";

import {
  RouteAnnouncer,
  titleForPathname,
} from "@/components/system/RouteAnnouncer";

describe("titleForPathname", () => {
  it("maps static routes to their section titles", () => {
    expect(titleForPathname("/")).toBe("Overview");
    expect(titleForPathname("/artists")).toBe("Artists");
    expect(titleForPathname("/search")).toBe("Search");
    expect(titleForPathname("/browse")).toBe("Browse");
    expect(titleForPathname("/review")).toBe("Review");
    expect(titleForPathname("/import")).toBe("Add from folder");
    expect(titleForPathname("/review/bank/abc123")).toBe("Review decision");
    expect(titleForPathname("/settings")).toBe("Settings");
    expect(titleForPathname("/duplicates")).toBe("Duplicates");
    expect(titleForPathname("/playlists")).toBe("Playlists");
  });

  it("decodes the artist segment into the title", () => {
    expect(titleForPathname("/artists/Daft%20Punk")).toBe("Daft Punk");
  });

  it("falls back to the section title on a malformed artist escape", () => {
    expect(titleForPathname("/artists/%E0%A4%A")).toBe("Artists");
  });

  it("uses static section titles for opaque dynamic segments", () => {
    expect(titleForPathname("/albums/42")).toBe("Album");
    expect(titleForPathname("/import/albums/3")).toBe("Import decision");
    expect(titleForPathname("/import/albums/3/duplicate")).toBe(
      "Import decision",
    );
    expect(titleForPathname("/settings/integrations")).toBe("Settings");
    expect(titleForPathname("/playlists/abc-123")).toBe("Playlists");
  });

  it("titles unknown routes as Not found", () => {
    expect(titleForPathname("/nope")).toBe("Not found");
  });
});

/** Page stub with the h1 contract PageHeader provides (tabIndex={-1}). */
function PageStub({
  title,
  linkTo,
  linkLabel,
}: {
  title: string;
  linkTo: string;
  linkLabel: string;
}) {
  return (
    <main>
      <h1 tabIndex={-1}>{title}</h1>
      <Link to={linkTo}>{linkLabel}</Link>
    </main>
  );
}

function renderAnnouncer(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <RouteAnnouncer />
      <Routes>
        <Route
          path="/"
          element={
            <PageStub
              title="Overview"
              linkTo="/artists"
              linkLabel="Go to artists"
            />
          }
        />
        <Route
          path="/artists"
          element={<PageStub title="Artists" linkTo="/" linkLabel="Go home" />}
        />
        <Route
          path="/browse"
          element={
            <PageStub
              title="Browse"
              linkTo="/browse?genre=rock"
              linkLabel="Filter rock"
            />
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("RouteAnnouncer", () => {
  beforeEach(() => {
    document.title = "";
  });

  it("sets the document title on first render without stealing focus", () => {
    renderAnnouncer("/");
    expect(document.title).toBe("Overview — MusicDrop");
    expect(
      screen.getByRole("heading", { level: 1, name: "Overview" }),
    ).not.toHaveFocus();
  });

  it("retitles, announces and focuses the page h1 on navigation", async () => {
    const user = userEvent.setup();
    renderAnnouncer("/");
    await user.click(screen.getByRole("link", { name: "Go to artists" }));
    expect(document.title).toBe("Artists — MusicDrop");
    expect(screen.getByRole("status")).toHaveTextContent("Artists");
    expect(
      screen.getByRole("heading", { level: 1, name: "Artists" }),
    ).toHaveFocus();
  });

  it("does not move focus when only search params change", async () => {
    const user = userEvent.setup();
    renderAnnouncer("/browse");
    await user.click(screen.getByRole("link", { name: "Filter rock" }));
    expect(
      screen.getByRole("heading", { level: 1, name: "Browse" }),
    ).not.toHaveFocus();
    expect(document.title).toBe("Browse — MusicDrop");
  });
});
