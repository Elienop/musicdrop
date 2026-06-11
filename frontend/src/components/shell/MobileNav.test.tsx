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

  test("the current route's item gets aria-current and the violet TEXT (no pill)", async () => {
    const user = userEvent.setup();
    renderWithProviders(<MobileNav />, { route: "/artists" });

    await user.click(screen.getByRole("button", { name: "Open navigation" }));

    const active = await screen.findByRole("link", { name: "Artists" });
    expect(active).toHaveAttribute("aria-current", "page");
    // Same dialect as the sidebar pill — announced AND visually styled.
    expect(active).toHaveClass("text-primary-light");
    expect(active.className).not.toMatch(/bg-primary/);
    expect(screen.getByRole("link", { name: "Browse" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  test("renders its own 44px close button instead of the inherited one", async () => {
    const user = userEvent.setup();
    renderWithProviders(<MobileNav />);

    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    await screen.findByRole("dialog");

    // The inherited DialogContent close (accessible name exactly "Close")
    // is suppressed — it's a sub-44px target.
    expect(
      screen.queryByRole("button", { name: "Close" }),
    ).not.toBeInTheDocument();
    // The drawer's own close button is a 44px (size-11) target…
    const close = screen.getByRole("button", { name: "Close navigation" });
    expect(close).toHaveClass("size-11");
    // …and actually closes the sheet.
    await user.click(close);
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });
});
