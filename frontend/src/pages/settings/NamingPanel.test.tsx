import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { client } from "@/api/client";
import type { components, operations } from "@/api/schema";
import { NamingPanel } from "@/pages/settings/NamingPanel";

type NamingSave422 =
  operations["save_naming_route_api_config_naming_save_post"]["responses"][422]["content"]["application/json"];
type ErrorDetail = components["schemas"]["ErrorDetail"];

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
  const def = await screen.findByDisplayValue(/\$albumartist/);
  // Save is gated on a real change — make one so the button enables.
  await userEvent.type(def, "X");
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

test("Save is disabled until the config actually changes", async () => {
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  const saveBtn = screen.getByRole("button", { name: /save naming/i });
  // No edits yet -> nothing to save, so the button is inert (no more spamming
  // an unchanged Save).
  expect(saveBtn).toBeDisabled();
  await userEvent.type(def, "X");
  expect(saveBtn).toBeEnabled();
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

// The recommended-rules button maps MusicBrainz typographic Unicode (the
// blink‐182 twin class) to ASCII. Patterns are stored as literal \uXXXX text
// (JS source escapes the backslash) so they stay legible in the editor.
test("Add recommended rules seeds the five typographic replace rows", async () => {
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.click(
    screen.getByRole("button", { name: /add recommended rules/i }),
  );
  expect(screen.getAllByLabelText(/replace pattern/i)).toHaveLength(5);
  // Exact pattern strings reach the inputs verbatim.
  expect(
    screen.getByDisplayValue("[\\u2010\\u2011\\u2212]"),
  ).toBeInTheDocument();
  expect(screen.getByDisplayValue("\\u2026")).toBeInTheDocument();
});

test("Add recommended rules is idempotent (clicking twice adds no duplicates)", async () => {
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  const btn = screen.getByRole("button", { name: /add recommended rules/i });
  await userEvent.click(btn);
  await userEvent.click(btn);
  expect(screen.getAllByLabelText(/replace pattern/i)).toHaveLength(5);
});

test("Add recommended rules skips a recommended pattern already present", async () => {
  const seeded = {
    ...naming,
    replace: [{ pattern: "\\u2026", replacement: "..." }],
    sha256: "sha-seed",
  };
  vi.spyOn(client, "GET").mockImplementation(async (path: string) => {
    if (path === "/api/config/naming")
      return { data: seeded, response: { ok: true, status: 200 } } as never;
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
  await screen.findByDisplayValue(/\$albumartist/);
  // Exactly the seeded row is present before the click.
  expect(screen.getAllByLabelText(/replace pattern/i)).toHaveLength(1);
  await userEvent.click(
    screen.getByRole("button", { name: /add recommended rules/i }),
  );
  // 1 seeded + 4 new (the ellipsis is deduped by exact pattern) = 5.
  expect(screen.getAllByLabelText(/replace pattern/i)).toHaveLength(5);
  expect(screen.getAllByDisplayValue("\\u2026")).toHaveLength(1);
});

test("Save includes the recommended rules once added", async () => {
  wrap(<NamingPanel />);
  await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.click(
    screen.getByRole("button", { name: /add recommended rules/i }),
  );
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  await waitFor(() =>
    expect(client.POST).toHaveBeenCalledWith(
      "/api/config/naming/save",
      expect.objectContaining({
        body: expect.objectContaining({
          replace: expect.arrayContaining([
            { pattern: "[\\u2010\\u2011\\u2212]", replacement: "-" },
            { pattern: "\\u2026", replacement: "..." },
          ]),
        }),
      }),
    ),
  );
});

/** Render with a saved-but-unapplied config and an Apply that fails with
 * `status` and `error`, click Apply, and return the alert it raises. */
async function applyFails(status: number, error: unknown) {
  // The beforeEach stubs, re-typed to the one argument they read.
  type Stub = (path: string) => Promise<unknown>;
  const get = vi.mocked(client.GET).getMockImplementation() as unknown as
    | Stub
    | undefined;
  const post = vi.mocked(client.POST).getMockImplementation() as unknown as
    | Stub
    | undefined;
  if (!get || !post) throw new Error("beforeEach mocks missing");
  vi.mocked(client.GET).mockImplementation((async (path: string) =>
    path === "/api/config"
      ? { data: { apply_pending: true }, response: { ok: true, status: 200 } }
      : get(path)) as never);
  vi.mocked(client.POST).mockImplementation((async (path: string) =>
    path === "/api/config/apply"
      ? { data: undefined, error, response: { ok: false, status } }
      : post(path)) as never);
  wrap(<NamingPanel />);
  const applyBtn = await screen.findByRole("button", { name: "Apply" });
  await waitFor(() => expect(applyBtn).toBeEnabled());
  await userEvent.click(applyBtn);
  return screen.findByRole("alert");
}

test("an Apply 422 shows the server's recovery sentence, not the restart advice", async () => {
  // The unreadable-file 422: boot reads the same file, so "restart" would
  // stop MusicDrop. The panel must print what the server says.
  const alert = await applyFails(422, {
    detail: {
      message: "config.yaml could not be read",
      recovery:
        "beets could not read config.yaml, so nothing was changed. Fix the file and Apply again.",
    },
  });
  expect(alert).toHaveTextContent(
    /^Apply failed\. beets could not read config\.yaml, so nothing was changed\. Fix the file and Apply again\.$/,
  );
});

test("an Apply failure without a recovery line falls back to the fixed sentence", async () => {
  const alert = await applyFails(422, { detail: "unexpected" });
  expect(alert).toHaveTextContent(
    /^Apply failed\. Your config is saved on disk; try again or restart MusicDrop\.$/,
  );
});

/** Edit the default template, click Save against a Save 422 answering `error`,
 * and return the alert it raises. */
async function saveFails(error: NamingSave422) {
  type Stub = (path: string) => Promise<unknown>;
  const post = vi.mocked(client.POST).getMockImplementation() as unknown as
    | Stub
    | undefined;
  if (!post) throw new Error("beforeEach mocks missing");
  vi.mocked(client.POST).mockImplementation((async (path: string) =>
    path === "/api/config/naming/save"
      ? { data: undefined, error, response: { ok: false, status: 422 } }
      : post(path)) as never);
  wrap(<NamingPanel />);
  const def = await screen.findByDisplayValue(/\$albumartist/);
  await userEvent.type(def, "X");
  await userEvent.click(screen.getByRole("button", { name: /save naming/i }));
  return screen.findByRole("alert");
}

test("a Save 422 about config.yaml on disk shows the server's sentence", async () => {
  const alert = await saveFails({
    detail: [
      {
        loc: "",
        msg: "config.yaml could not be written: Permission denied.",
        type: "config_on_disk",
      },
    ],
  });
  expect(alert).toHaveTextContent(
    /^Save failed: config\.yaml could not be written: Permission denied\.$/,
  );
});

test("a Save 422 for a bad replace: pattern keeps the fixed sentence", async () => {
  const alert = await saveFails({
    detail: [
      {
        loc: "replace[0]",
        msg: "unterminated character set at position 0",
        type: "value_error",
      },
    ],
  });
  expect(alert).toHaveTextContent(/^Save failed: Failed to save naming config$/);
});

/** Render against a naming GET that answers `status` + `error`; return the alert. */
async function loadFails(status: number, error: ErrorDetail) {
  vi.mocked(client.GET).mockImplementation((async () => ({
    data: undefined,
    error,
    response: { ok: false, status },
  })) as never);
  wrap(<NamingPanel />);
  return screen.findByRole("alert");
}

test("a load 422 about config.yaml on disk shows the server's sentence", async () => {
  const alert = await loadFails(422, {
    detail: "config.yaml could not be read: No such file or directory.",
  });
  expect(alert).toHaveTextContent(
    /^config\.yaml could not be read: No such file or directory\.$/,
  );
});

test("a load 500 keeps the fixed sentence", async () => {
  const alert = await loadFails(500, { detail: "Internal Server Error" });
  expect(alert).toHaveTextContent(/^Could not load naming config\.$/);
});

test("a load network failure keeps the fixed sentence", async () => {
  vi.mocked(client.GET).mockRejectedValue(new TypeError("Failed to fetch"));
  wrap(<NamingPanel />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    /^Could not load naming config\.$/,
  );
});
