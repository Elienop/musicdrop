import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { client } from "@/api/client";
import { NamingPanel } from "@/pages/settings/NamingPanel";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const naming = {
  default: "$albumartist/$album/$track $title",
  comp: null,
  singleton: null,
  custom: [],
  replace: [],
  sha256: "sha-1",
  previews: [
    {
      query: "default",
      sample_path: "Adele/25/01 Hello.flac",
      sample_source: "Adele — 25",
      error: null,
    },
  ],
  replace_errors: [],
};

beforeEach(() => {
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/config/naming")
      return { data: naming, response: { ok: true, status: 200 } } as never;
    if (path === "/api/imports/active")
      return {
        data: { active: false },
        response: { ok: true, status: 200 },
      } as never;
    if (path === "/api/config")
      return {
        data: { apply_pending: false },
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
  vi.spyOn(client, "POST").mockImplementation(async (path: string) => {
    if (path === "/api/config/naming/preview")
      return {
        data: { rendered: naming.previews, replace_errors: [] },
        response: { ok: true, status: 200 },
      } as never;
    if (path === "/api/config/naming/save")
      return {
        data: { apply_pending: true },
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });
});

afterEach(() => vi.restoreAllMocks());

test("renders current default + its live preview", async () => {
  wrap(<NamingPanel />);
  expect(
    await screen.findByDisplayValue(/\$albumartist\/\$album/),
  ).toBeInTheDocument();
  expect(screen.getByText(/Adele\/25\/01 Hello\.flac/)).toBeInTheDocument();
});

test("Save posts the assembled rules with the base sha", async () => {
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  await waitFor(() =>
    expect(client.POST).toHaveBeenCalledWith(
      "/api/config/naming/save",
      expect.objectContaining({
        body: expect.objectContaining({ base_sha256: "sha-1" }),
      }),
    ),
  );
});

test("Add rule reveals a custom query input", async () => {
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.click(screen.getByRole("button", { name: /add rule/i }));
  expect(screen.getByLabelText(/custom rule 1 query/i)).toBeInTheDocument();
});

// With no explicit paths:/replace: on disk, the backend now returns beets'
// effective defaults (default/comp/singleton + replace rows). The panel must
// pre-fill those — not blanks — so it's a faithful, editable view of the
// active naming.
test("pre-fills with beets' effective defaults (no-override case)", async () => {
  const effective = {
    default: "$albumartist/$album%aunique{}/$track $title",
    comp: "Compilations/$album%aunique{}/$track $title",
    singleton: "Non-Album/$artist/$title",
    custom: [],
    replace: [
      { pattern: "[<>:\\?\\*\\|]", replacement: "_" },
      { pattern: "^-", replacement: "_" },
      { pattern: "\\s+$", replacement: "" },
    ],
    sha256: "sha-eff",
    previews: [],
    replace_errors: [],
  };
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/config/naming")
      return { data: effective, response: { ok: true, status: 200 } } as never;
    if (path === "/api/imports/active")
      return {
        data: { active: false },
        response: { ok: true, status: 200 },
      } as never;
    if (path === "/api/config")
      return {
        data: { apply_pending: false },
        response: { ok: true, status: 200 },
      } as never;
    return { data: undefined, response: { ok: false, status: 404 } } as never;
  });

  wrap(<NamingPanel />);

  expect(
    await screen.findByDisplayValue(
      "$albumartist/$album%aunique{}/$track $title",
    ),
  ).toBeInTheDocument();
  expect(
    screen.getByDisplayValue("Compilations/$album%aunique{}/$track $title"),
  ).toBeInTheDocument();
  expect(
    screen.getByDisplayValue("Non-Album/$artist/$title"),
  ).toBeInTheDocument();
  // A bundled replace row pre-fills too (no blank panel).
  expect(screen.getByDisplayValue("^-")).toBeInTheDocument();
});
