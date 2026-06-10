import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router";
import { describe, expect, test } from "vitest";

import { MobileNav } from "@/components/shell/MobileNav";
import { renderWithProviders } from "@/test/render";

/** Exposes the live router location so tests can assert navigation. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

describe("MobileNav", () => {
  test("renders a hamburger trigger and no sheet until opened", () => {
    renderWithProviders(<MobileNav />);

    expect(
      screen.getByRole("button", { name: "Open navigation" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  test("opens a sheet listing every section and item", async () => {
    const user = userEvent.setup();
    renderWithProviders(<MobileNav />);

    await user.click(screen.getByRole("button", { name: "Open navigation" }));

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
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
    // No dead slskd links here either (spec §1).
    expect(
      screen.queryByRole("link", { name: "Find music" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Downloads" }),
    ).not.toBeInTheDocument();
    // ≥44px touch targets in the drawer.
    expect(screen.getByRole("link", { name: "Artists" })).toHaveClass("h-11");
  });

  test("navigating from the sheet closes it", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <>
        <MobileNav />
        <LocationProbe />
      </>,
    );

    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    await user.click(await screen.findByRole("link", { name: "Artists" }));

    expect(screen.getByTestId("location")).toHaveTextContent("/artists");
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });
});
