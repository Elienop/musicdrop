import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test } from "vitest";

import { AppSidebar, sectionForPathname } from "@/components/shell/Sidebar";
import { renderWithProviders } from "@/test/render";

beforeEach(() => {
  window.localStorage.clear();
});

describe("sectionForPathname", () => {
  test("maps nav routes to their sections (exact for /, prefix otherwise)", () => {
    expect(sectionForPathname("/", null)).toBe("Library");
    expect(sectionForPathname("/artists/Adele", null)).toBe("Library");
    expect(sectionForPathname("/browse", null)).toBe("Library");
    expect(sectionForPathname("/review", null)).toBe("Acquire");
    expect(sectionForPathname("/import", null)).toBe("Acquire");
    expect(sectionForPathname("/settings/anything", null)).toBe("Manage");
  });

  test("albums and search default to Library", () => {
    expect(sectionForPathname("/albums/9", null)).toBe("Library");
    expect(sectionForPathname("/search", null)).toBe("Library");
  });

  test("an album entered from Review lights Acquire via its origin state", () => {
    expect(
      sectionForPathname("/albums/9", {
        from: { label: "Review", to: "/review" },
      }),
    ).toBe("Acquire");
  });

  test("malformed origin state falls back to Library", () => {
    expect(sectionForPathname("/albums/9", { from: { to: 7 } })).toBe(
      "Library",
    );
    expect(sectionForPathname("/albums/9", "junk")).toBe("Library");
    expect(sectionForPathname("/albums/9", { from: null })).toBe("Library");
  });
});

describe("AppSidebar", () => {
  test("renders every section and item with its route; no dead slskd links", () => {
    renderWithProviders(<AppSidebar />);

    for (const label of ["Library", "Acquire", "Manage"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    const expected: Array<[string, string]> = [
      ["Overview", "/"],
      ["Artists", "/artists"],
      ["Browse", "/browse"],
      ["Review", "/review"],
      ["Add from folder", "/import"],
      ["Playlists", "/playlists"],
      ["Duplicates", "/duplicates"],
      ["Settings", "/settings"],
    ];
    for (const [name, href] of expected) {
      expect(screen.getByRole("link", { name })).toHaveAttribute("href", href);
    }
    // Find music / Downloads are designed slots for the slskd project — they
    // must NOT render as links yet (spec §1: no dead links).
    expect(
      screen.queryByRole("link", { name: "Find music" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Downloads" }),
    ).not.toBeInTheDocument();
  });

  test("the brand is a link to /, not a heading", () => {
    renderWithProviders(<AppSidebar />);
    expect(screen.getByRole("link", { name: "MusicDrop" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });

  test("the current route's item gets aria-current, violet text, and non-color cues", () => {
    renderWithProviders(<AppSidebar />, { route: "/artists" });

    const active = screen.getByRole("link", { name: "Artists" });
    expect(active).toHaveAttribute("aria-current", "page");
    // aria-current must be visually styled, not just announced (spec §1) —
    // and not by hue alone (WCAG 1.4.1): weight + a left indicator bar.
    expect(active).toHaveClass("text-primary-light");
    expect(active).toHaveClass("font-medium");
    expect(
      active.querySelector('span[aria-hidden="true"].bg-primary-light'),
    ).not.toBeNull();
    // Still no pill background on the link itself.
    expect(active.className).not.toMatch(/bg-primary/);
    // ≥44px hit area (h-11).
    expect(active).toHaveClass("h-11");
    const inactive = screen.getByRole("link", { name: "Overview" });
    expect(inactive).not.toHaveAttribute("aria-current");
    expect(inactive).not.toHaveClass("font-medium");
    expect(inactive.querySelector(".bg-primary-light")).toBeNull();
  });

  test("an album page lights its origin section, with no item current", () => {
    const { container } = renderWithProviders(<AppSidebar />, {
      route: {
        pathname: "/albums/9",
        state: { from: { label: "Review", to: "/review" } },
      },
    });

    expect(container.querySelector('[data-section="Acquire"]')).toHaveAttribute(
      "data-active",
      "true",
    );
    expect(
      container.querySelector('[data-section="Library"]'),
    ).not.toHaveAttribute("data-active");
    expect(
      container.querySelector('[aria-current="page"]'),
    ).not.toBeInTheDocument();
  });

  test("collapse toggles the rail and persists to localStorage", async () => {
    const user = userEvent.setup();
    const { container } = renderWithProviders(<AppSidebar />);

    expect(container.querySelector("aside")).toHaveAttribute(
      "data-collapsed",
      "false",
    );

    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));

    expect(container.querySelector("aside")).toHaveAttribute(
      "data-collapsed",
      "true",
    );
    expect(window.localStorage.getItem("md.sidebar.collapsed")).toBe("1");
    const expand = screen.getByRole("button", { name: "Expand sidebar" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    // Rail mode: group label is visually hidden but still announced; items
    // keep their accessible names via aria-label.
    expect(screen.getByText("Library")).toHaveClass("sr-only");
    expect(screen.getByRole("link", { name: "Artists" })).toBeInTheDocument();

    await user.click(expand);
    expect(window.localStorage.getItem("md.sidebar.collapsed")).toBe("0");
    expect(
      screen.getByRole("button", { name: "Collapse sidebar" }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  test("a persisted collapsed state is restored on mount", () => {
    window.localStorage.setItem("md.sidebar.collapsed", "1");
    const { container } = renderWithProviders(<AppSidebar />);

    expect(container.querySelector("aside")).toHaveAttribute(
      "data-collapsed",
      "true",
    );
    expect(
      screen.getByRole("button", { name: "Expand sidebar" }),
    ).toBeInTheDocument();
  });

  test("renders a count badge in the Review slot", () => {
    renderWithProviders(<AppSidebar badges={{ Review: 3 }} />);

    const review = screen.getByRole("link", { name: "Review (3)" });
    expect(review).toHaveAttribute("href", "/review");
    expect(review).toHaveTextContent("3");
  });

  test("a zero badge renders nothing extra", () => {
    renderWithProviders(<AppSidebar badges={{ Review: 0 }} />);
    expect(screen.getByRole("link", { name: "Review" })).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });
});
