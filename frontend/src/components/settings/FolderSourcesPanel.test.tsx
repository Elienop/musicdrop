import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { FolderSourcesPanel } from "@/components/settings/FolderSourcesPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const SOURCES_URL = `${window.location.origin}/api/sources`;
const ADD_URL = `${window.location.origin}/api/sources/folders`;
const REMOVE_URL = `${window.location.origin}/api/sources/folders/:id`;
const FOLDERS_URL = `${window.location.origin}/api/folders`;

type Row = { id: string; name: string; folder: string; exists: boolean };

function row(id: string, name: string, exists = true): Row {
  return { id, name, folder: `/media/downloads/${name}`, exists };
}

/** A server holding `rows`: GET lists them, DELETE drops one (204). */
function serve(rows: Row[]) {
  let list = [...rows];
  server.use(
    http.get(SOURCES_URL, () => HttpResponse.json({ sources: list })),
    http.delete(REMOVE_URL, ({ params }) => {
      list = list.filter((r) => r.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
  );
}

/** A response held until the test opens it, to act while a request is in
 * flight. */
function gate() {
  let open!: () => void;
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { open, opened };
}

function removeButton(name: string) {
  return screen.getByRole("button", { name: `Remove ${name} from Sources` });
}

describe("FolderSourcesPanel: the list", () => {
  test("each row: name, folder, and Missing only when it is not there", async () => {
    serve([row("a", "yubal"), row("b", "lidarr", false)]);
    renderWithProviders(<FolderSourcesPanel />);

    const section = screen.getByRole("region", { name: "Folders" });
    expect(
      within(section).getByText(
        "Folders you import from often. Each gets a button on Add from folder.",
      ),
    ).toBeInTheDocument();
    const items = await within(section).findAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("yubal/media/downloads/yubal");
    expect(within(items[0]!).queryByText("Missing")).toBeNull();
    // Missing sits on the name line, before the path.
    expect(items[1]).toHaveTextContent("lidarrMissing/media/downloads/lidarr");
    expect(within(items[1]!).getByText("Missing")).toBeInTheDocument();
    expect(removeButton("lidarr")).toBeInTheDocument();
  });

  test("an empty list says so", async () => {
    serve([]);
    renderWithProviders(<FolderSourcesPanel />);
    expect(await screen.findByText("No folders yet.")).toBeInTheDocument();
    expect(screen.queryByRole("list")).toBeNull();
  });
});

describe("FolderSourcesPanel: adding", () => {
  test("sends the trimmed name and folder, lists the new row, and clears the row", async () => {
    let body: unknown = null;
    let list: Row[] = [];
    server.use(
      http.get(SOURCES_URL, () => HttpResponse.json({ sources: list })),
      http.post(ADD_URL, async ({ request }) => {
        body = await request.json();
        const added = row("n", "yubal");
        list = [added];
        return HttpResponse.json(added, { status: 201 });
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findByText("No folders yet.");

    const name = screen.getByLabelText("Name");
    expect(name).toHaveAttribute("placeholder", "yubal");
    expect(name).toHaveAttribute("maxLength", "40");
    const folder = screen.getByRole("textbox", { name: "Folder" });
    expect(folder).toHaveAttribute("placeholder", "/media/downloads/yubal");
    const add = screen.getByRole("button", { name: "Add" });
    expect(add).toBeDisabled();

    await user.type(name, "  yubal ");
    await user.type(folder, " /media/downloads/yubal  ");
    await user.click(add);

    await waitFor(() =>
      expect(body).toEqual({ name: "yubal", folder: "/media/downloads/yubal" }),
    );
    expect(await screen.findByRole("listitem")).toHaveTextContent("yubal");
    expect(name).toHaveValue("");
    expect(folder).toHaveValue("");
    // Add went disabled with the fields; the next add starts at Name.
    expect(name).toHaveFocus();
  });

  test.each([
    "That folder doesn’t exist.",
    "That folder is your library or holds it. Pick another.",
    "That’s slskd’s whole folder. Pick an album inside it.",
  ])("the server's refusal shows in its words: %s", async (sentence) => {
    serve([]);
    server.use(
      http.post(ADD_URL, () =>
        HttpResponse.json({ detail: sentence }, { status: 422 }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findByText("No folders yet.");

    const folder = screen.getByRole("textbox", { name: "Folder" });
    await user.type(screen.getByLabelText("Name"), "x");
    await user.type(folder, "/media/x");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(folder).toHaveAttribute("aria-invalid", "true");
    expect(folder).toHaveAccessibleDescription(sentence);
    // Editing the field clears the stale refusal.
    await user.type(folder, "y");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(folder).toHaveAttribute("aria-invalid", "false");
  });

  test("FastAPI's own 422 is machine copy: the generic sentence, field not blamed", async () => {
    serve([]);
    server.use(
      http.post(ADD_URL, () =>
        HttpResponse.json(
          { detail: [{ msg: "String should have at most 40 characters" }] },
          { status: 422 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findByText("No folders yet.");

    await user.type(screen.getByLabelText("Name"), "x");
    await user.type(screen.getByRole("textbox", { name: "Folder" }), "/m");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn’t add that folder. Try again.",
    );
    expect(screen.getByRole("textbox", { name: "Folder" })).toHaveAttribute(
      "aria-invalid",
      "false",
    );
  });

  test.each<[string, number, string, string, boolean]>([
    [
      "a 409 is about the folder: its words, field blamed",
      409,
      "Two folders display under the same name because their names are not valid UTF-8. Rename one on disk to tell them apart.",
      "Two folders display under the same name because their names are not valid UTF-8. Rename one on disk to tell them apart.",
      true,
    ],
    [
      "a 503 is about the layout: its words, field not blamed",
      503,
      "the music folder is not mounted; imports are refused",
      "the music folder is not mounted; imports are refused.",
      false,
    ],
  ])("%s", async (_, status, detail, shown, blamed) => {
    serve([]);
    server.use(
      http.post(ADD_URL, () => HttpResponse.json({ detail }, { status })),
    );
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findByText("No folders yet.");

    const folder = screen.getByRole("textbox", { name: "Folder" });
    await user.type(screen.getByLabelText("Name"), "x");
    await user.type(folder, "/media/x");
    await user.click(screen.getByRole("button", { name: "Add" }));

    // Whole string: the generic sentence must not stand in for the server's.
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(shown);
    expect(folder).toHaveAttribute("aria-invalid", String(blamed));
    if (blamed) expect(folder).toHaveAccessibleDescription(shown);
    else expect(folder).not.toHaveAttribute("aria-describedby");
  });

  test("typing while an add is in flight never detaches it: a 422 still shows", async () => {
    const sentence = "That folder doesn’t exist.";
    const held = gate();
    serve([]);
    server.use(
      http.post(ADD_URL, async () => {
        await held.opened;
        return HttpResponse.json({ detail: sentence }, { status: 422 });
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findByText("No folders yet.");

    const folder = screen.getByRole("textbox", { name: "Folder" });
    await user.type(screen.getByLabelText("Name"), "x");
    await user.type(folder, "/media/x");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await screen.findByRole("button", { name: "Adding…" });

    await user.type(folder, "y");
    expect(screen.getByRole("button", { name: "Adding…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    held.open();

    expect(await screen.findByRole("alert")).toHaveTextContent(sentence);
    expect(folder).toHaveAttribute("aria-invalid", "true");
  });

  test("typing while an add is in flight never detaches it: a 201 still clears the row and focuses Name", async () => {
    const held = gate();
    let list: Row[] = [];
    server.use(
      http.get(SOURCES_URL, () => HttpResponse.json({ sources: list })),
      http.post(ADD_URL, async () => {
        await held.opened;
        const added = row("n", "x");
        list = [added];
        return HttpResponse.json(added, { status: 201 });
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findByText("No folders yet.");

    const name = screen.getByLabelText("Name");
    const folder = screen.getByRole("textbox", { name: "Folder" });
    await user.type(name, "x");
    await user.type(folder, "/media/downloads/x");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await screen.findByRole("button", { name: "Adding…" });

    // Name first, so each field's handler is exercised; focus ends on Folder.
    await user.type(name, "y");
    await user.type(folder, "y");
    held.open();

    await waitFor(() => expect(name).toHaveValue(""));
    expect(folder).toHaveValue("");
    expect(name).toHaveFocus();
    expect(await screen.findByRole("listitem")).toHaveTextContent("x");
  });

  test("Browse fills the Folder field", async () => {
    serve([]);
    server.use(
      http.get(FOLDERS_URL, () =>
        HttpResponse.json({
          path: "/media",
          parent: "/",
          folders: [],
          total: 0,
          refusal: null,
        }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findByText("No folders yet.");

    await user.click(screen.getByRole("button", { name: "Browse folders" }));
    const dialog = await screen.findByRole("dialog", { name: "Choose a folder" });
    await within(dialog).findByText("No subfolders.");
    await user.click(
      within(dialog).getByRole("button", { name: "Use this folder" }),
    );

    const folder = screen.getByRole("textbox", { name: "Folder" });
    await waitFor(() => expect(folder).toHaveValue("/media"));
    expect(folder).toHaveFocus();
  });
});

describe("FolderSourcesPanel: focus after a remove", () => {
  test.each<[string, string[], string, string]>([
    ["the next row's remove button", ["a", "b", "c"], "b", "Remove c from Sources"],
    ["else the previous one", ["a", "b", "c"], "c", "Remove b from Sources"],
  ])("%s", async (_, names, removed, focused) => {
    serve(names.map((n) => row(n, n)));
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findAllByRole("listitem");

    await user.click(removeButton(removed));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: focused })).toHaveFocus(),
    );
    expect(
      screen.queryByRole("button", { name: `Remove ${removed} from Sources` }),
    ).toBeNull();
  });

  test("else the Add row's Name", async () => {
    serve([row("a", "a")]);
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findByRole("listitem");

    await user.click(removeButton("a"));

    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveFocus());
    expect(await screen.findByText("No folders yet.")).toBeInTheDocument();
  });

  test("a source already gone (404) is removed all the same", async () => {
    // Another tab removed `a` after this page listed it.
    let list = [row("a", "a"), row("b", "b")];
    server.use(
      http.get(SOURCES_URL, () => {
        const listed = list;
        list = [row("b", "b")];
        return HttpResponse.json({ sources: listed });
      }),
      http.delete(REMOVE_URL, () =>
        HttpResponse.json({ detail: "Source not found." }, { status: 404 }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findAllByRole("listitem");

    await user.click(removeButton("a"));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Remove b from Sources" })).toHaveFocus(),
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("a failed remove keeps the row and says so", async () => {
    serve([row("a", "a")]);
    server.use(
      http.delete(REMOVE_URL, () => new HttpResponse(null, { status: 500 })),
    );
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findByRole("listitem");

    await user.click(removeButton("a"));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn’t remove that source. Try again.",
    );
    expect(removeButton("a")).toHaveFocus();
  });

  test("a dead network reads our sentence, not the browser's", async () => {
    serve([row("a", "a")]);
    server.use(http.delete(REMOVE_URL, () => HttpResponse.error()));
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findByRole("listitem");

    await user.click(removeButton("a"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Couldn’t remove that source. Try again.");
  });

  test("a user who moved on keeps their place", async () => {
    const held = gate();
    let list = [row("a", "a"), row("b", "b")];
    server.use(
      http.get(SOURCES_URL, () => HttpResponse.json({ sources: list })),
      http.delete(REMOVE_URL, async ({ params }) => {
        await held.opened;
        list = list.filter((r) => r.id !== params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<FolderSourcesPanel />);
    await screen.findAllByRole("listitem");

    await user.click(removeButton("a"));
    const name = screen.getByLabelText("Name");
    await user.click(name);
    held.open();

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Remove a from Sources" }),
      ).toBeNull(),
    );
    expect(name).toHaveFocus();
  });
});
