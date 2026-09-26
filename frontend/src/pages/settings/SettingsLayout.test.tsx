import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, Navigate, RouterProvider } from "react-router";
import { describe, expect, test } from "vitest";

import { SettingsLayout } from "@/pages/settings/SettingsLayout";

/** Mounts the layout with stub section bodies — nav anatomy + redirect
 * mechanics only. The REAL beets section rides the layout in
 * SettingsBeetsPage.test.tsx (whose renderPage starts at /settings). */
function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/settings",
        element: <SettingsLayout />,
        children: [
          { index: true, element: <Navigate to="/settings/beets" replace /> },
          { path: "beets", element: <p>beets section body</p> },
          { path: "naming", element: <p>naming section body</p> },
          { path: "metadata", element: <p>metadata section body</p> },
          { path: "sources", element: <p>sources section body</p> },
          { path: "integrations", element: <p>integrations section body</p> },
          { path: "trash", element: <p>trash section body</p> },
          { path: "account", element: <p>account section body</p> },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  return render(<RouterProvider router={router} />);
}

describe("SettingsLayout", () => {
  test("renders the one Settings h1, focusable for RouteAnnouncer", () => {
    renderAt("/settings/beets");
    const h1 = screen.getByRole("heading", { level: 1, name: "Settings" });
    expect(h1).toHaveAttribute("tabindex", "-1");
  });

  test("renders the seven section links in the sub-nav, in order", () => {
    renderAt("/settings/beets");
    const nav = screen.getByRole("navigation", { name: "Settings sections" });
    const links = within(nav).getAllByRole("link");
    // The ORDER is asserted, not just the membership: the rail is read
    // top-to-bottom and Account is deliberately last — it is the one section
    // about the operator rather than about the library.
    expect(links.map((l) => l.getAttribute("href"))).toEqual([
      "/settings/beets",
      "/settings/naming",
      "/settings/metadata",
      "/settings/sources",
      "/settings/integrations",
      "/settings/trash",
      "/settings/account",
    ]);
  });

  test.each([
    ["/settings/beets", "Beets"],
    ["/settings/naming", "Naming"],
    ["/settings/metadata", "Metadata"],
    ["/settings/sources", "Sources"],
    ["/settings/integrations", "Integrations"],
    ["/settings/trash", "Trash"],
    ["/settings/account", "Account"],
  ])("marks exactly one active section with aria-current at %s", (path, label) => {
    renderAt(path);
    const active = screen.getByRole("link", { name: label });
    expect(active).toHaveAttribute("aria-current", "page");
    const current = screen
      .getAllByRole("link")
      .filter((l) => l.getAttribute("aria-current") === "page");
    expect(current).toEqual([active]);
  });

  test("renders the matched section into the outlet", () => {
    renderAt("/settings/naming");
    expect(screen.getByText("naming section body")).toBeInTheDocument();
  });

  test("/settings redirects to the beets section", () => {
    renderAt("/settings");
    expect(screen.getByText("beets section body")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Beets" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});
