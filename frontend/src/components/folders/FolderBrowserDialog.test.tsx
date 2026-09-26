import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { useRef, useState } from "react";
import { describe, expect, onTestFinished, test, vi } from "vitest";

import { createAppQueryClient } from "@/api/queryClient";
import type { FolderEntry, FolderListing } from "@/api/useFolders";
import { FolderBrowserDialog } from "@/components/folders/FolderBrowserDialog";
import { server } from "@/test/msw-server";
import { renderWithProviders } from "@/test/render";

const FOLDERS_URL = `${window.location.origin}/api/folders`;
const LIBRARY_PARENT = "That folder holds your library.";
const UNREADABLE = "That folder can’t be read. Permission denied.";

function entry(
  parent: string,
  name: string,
  badge: FolderEntry["badge"] = null,
): FolderEntry {
  return { name, path: `${parent === "/" ? "" : parent}/${name}`, badge };
}

function listing(
  path: string,
  parent: string | null,
  names: (string | [string, FolderEntry["badge"]])[],
  refusal: string | null = null,
): FolderListing {
  const folders = names.map((n) =>
    typeof n === "string" ? entry(path, n) : entry(path, n[0], n[1]),
  );
  return { path, parent, folders, total: folders.length, refusal };
}

/** `/media/big`, over the cap: the first 500 of 2,140 folders. */
function bigListing(refusal: string | null = null): FolderListing {
  const names = Array.from(
    { length: 500 },
    (_, i) => `f${String(i).padStart(3, "0")}`,
  );
  return { ...listing("/media/big", "/media", names, refusal), total: 2140 };
}

/** A small tree. `/media` holds the library, so it and `/` are refused. */
const TREE: Record<string, FolderListing> = {
  "/": listing("/", null, ["media"], LIBRARY_PARENT),
  "/media": listing(
    "/media",
    "/",
    ["downloads", ["music", "library"]],
    LIBRARY_PARENT,
  ),
  "/media/downloads": listing("/media/downloads", "/media", [
    "Artist - Album",
    "Locked",
    "Other Album",
    ["beets", "musicdrop"],
  ]),
  "/media/downloads/Artist - Album": listing(
    "/media/downloads/Artist - Album",
    "/media/downloads",
    [],
  ),
};

/** Serve {@link TREE}: no path → `/media`, a missing one → its nearest
 * listed parent, `Locked` → the unreadable 422. Returns what was asked. */
function serveTree(extra: Record<string, FolderListing> = {}) {
  const asked: (string | null)[] = [];
  const tree = { ...TREE, ...extra };
  server.use(
    http.get(FOLDERS_URL, ({ request }) => {
      const path = new URL(request.url).searchParams.get("path");
      asked.push(path);
      if (path === "/media/downloads/Locked") {
        return HttpResponse.json({ detail: UNREADABLE }, { status: 422 });
      }
      let key = path ?? "/media";
      while (!(key in tree)) key = key.slice(0, key.lastIndexOf("/")) || "/";
      return HttpResponse.json(tree[key]);
    }),
  );
  return asked;
}

/** The dialog beside a field it fills, as a page mounts it. */
function Host({
  initial = "",
  recent = null,
  onUse = () => {},
}: Readonly<{
  initial?: string;
  recent?: string | null;
  onUse?: (path: string) => void;
}>) {
  const [value, setValue] = useState(initial);
  const fieldRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        aria-label="Field"
        ref={fieldRef}
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <FolderBrowserDialog
        value={value}
        recent={recent}
        fieldRef={fieldRef}
        onUse={(path) => {
          setValue(path);
          onUse(path);
        }}
      />
    </>
  );
}

async function openBrowser(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Browse folders" }));
  return screen.findByRole("dialog", { name: "Choose a folder" });
}

function pathBox(dialog: HTMLElement) {
  return within(dialog).getByRole("textbox", { name: "Folder path" });
}

function useButton(dialog: HTMLElement) {
  return within(dialog).getByRole("button", { name: "Use this folder" });
}

describe("FolderBrowserDialog: where it opens", () => {
  test("at the field's folder", async () => {
    const asked = serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads/" />);

    const dialog = await openBrowser(user);

    expect(
      await within(dialog).findByRole("button", { name: "Artist - Album" }),
    ).toBeInTheDocument();
    expect(asked).toEqual(["/media/downloads"]);
    expect(pathBox(dialog)).toHaveValue("/media/downloads/");
  });

  test("at the newest Recent folder when the field is empty", async () => {
    const asked = serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host recent="/media/downloads" />);

    const dialog = await openBrowser(user);

    await within(dialog).findByRole("button", { name: "Artist - Album" });
    expect(asked[0]).toBe("/media/downloads");
  });

  test("at the server's default when both are empty, first row focused", async () => {
    const asked = serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host recent={null} />);

    const dialog = await openBrowser(user);

    const first = await within(dialog).findByRole("button", {
      name: "downloads",
    });
    expect(asked[0]).toBeNull();
    expect(pathBox(dialog)).toHaveValue("/media/");
    // The first folder row, not the path box: a phone keeps its keyboard down.
    await waitFor(() => expect(first).toHaveFocus());
  });

  test("a missing folder lists its nearest parent, asked for once", async () => {
    // The answer names another folder than the one asked for; the box then
    // shows it. Stored under its own path too, so the same listing is not
    // fetched a second time (the app's staleTime, not the test default of 0).
    const asked = serveTree();
    const user = userEvent.setup();
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
    });
    render(
      <QueryClientProvider client={client}>
        <Host initial="/media/gone" />
      </QueryClientProvider>,
    );

    const dialog = await openBrowser(user);

    await within(dialog).findByRole("button", { name: "downloads" });
    expect(pathBox(dialog)).toHaveValue("/media/");
    // A second request would go out as soon as the box names `/media`; give
    // it the time a mocked round trip takes before saying there was none.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(asked).toEqual(["/media/gone"]);
  });
});

describe("FolderBrowserDialog: moving around", () => {
  test("a row opens its folder and focus lands on the new first row", async () => {
    serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media" />);
    const dialog = await openBrowser(user);

    await user.click(
      await within(dialog).findByRole("button", { name: "downloads" }),
    );

    const first = await within(dialog).findByRole("button", {
      name: "Artist - Album",
    });
    await waitFor(() => expect(first).toHaveFocus());
    expect(pathBox(dialog)).toHaveValue("/media/downloads/");
  });

  test("Up returns focus to the row you came from", async () => {
    // From "Other Album", the third row, so the first row is a wrong answer.
    serveTree({
      "/media/downloads/Other Album": listing(
        "/media/downloads/Other Album",
        "/media/downloads",
        ["CD1"],
      ),
    });
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads/Other Album" />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "CD1" });

    await user.click(within(dialog).getByRole("button", { name: "Up" }));

    const cameFrom = await within(dialog).findByRole("button", {
      name: "Other Album",
    });
    await waitFor(() => expect(cameFrom).toHaveFocus());
    expect(pathBox(dialog)).toHaveValue("/media/downloads/");
  });

  test("there is no Up row at /", async () => {
    serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/" />);
    const dialog = await openBrowser(user);

    await within(dialog).findByRole("button", { name: "media" });
    expect(
      within(dialog).queryByRole("button", { name: "Up" }),
    ).not.toBeInTheDocument();
  });

  test("an empty folder says so", async () => {
    serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads/Artist - Album" />);
    const dialog = await openBrowser(user);

    expect(await within(dialog).findByText("No subfolders.")).toBeVisible();
  });

  test("the typed tail narrows by contains, ignoring case, with no request", async () => {
    const asked = serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "Artist - Album" });
    const before = asked.length;

    await user.type(pathBox(dialog), "ALB");

    expect(
      within(dialog).getByRole("button", { name: "Artist - Album" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "Other Album" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: "Locked" }),
    ).not.toBeInTheDocument();

    await user.type(pathBox(dialog), "zzz");

    expect(
      within(dialog).getByText("No folders match “ALBzzz”."),
    ).toBeVisible();
    expect(asked).toHaveLength(before);
  });

  test("Enter opens the first matching row", async () => {
    const asked = serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "Artist - Album" });

    await user.type(pathBox(dialog), "other{Enter}");

    await waitFor(() =>
      expect(asked).toContain("/media/downloads/Other Album"),
    );
  });

  test("Enter after a failed path opens nothing from the last list", async () => {
    const asked = serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "Artist - Album" });

    await user.type(pathBox(dialog), "Locked/");
    await within(dialog).findByRole("alert");
    const before = asked.length;
    await user.keyboard("{Enter}");

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(asked).toHaveLength(before);
  });

  test("Enter with nothing after the last / opens nothing", async () => {
    // The box names the open folder itself; the first row is not what was typed.
    const asked = serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "Artist - Album" });
    const before = asked.length;

    await user.click(pathBox(dialog));
    await user.keyboard("{Enter}");

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(asked).toHaveLength(before);
    expect(pathBox(dialog)).toHaveValue("/media/downloads/");
  });

  test("two folders that display alike are two rows, each keyed apart", async () => {
    // Undecodable names both read as U+FFFD, so their paths repeat too.
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    onTestFinished(() => error.mockRestore());
    serveTree({
      "/media/odd": listing("/media/odd", "/media", ["�", "�"]),
    });
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/odd" />);
    const dialog = await openBrowser(user);

    expect(
      await within(dialog).findAllByRole("button", { name: "�" }),
    ).toHaveLength(2);
    expect(error.mock.calls.flat().join(" ")).not.toMatch(/same key/);
  });

  test("badges name the library and MusicDrop's folders, and nothing else", async () => {
    serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media" />);
    const dialog = await openBrowser(user);

    expect(
      await within(dialog).findByRole("button", { name: "music Library" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "downloads" }),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "downloads" }));

    expect(
      await within(dialog).findByRole("button", { name: "beets MusicDrop" }),
    ).toBeInTheDocument();
  });

  test("over the cap, the line under the list says how many there are", async () => {
    serveTree({ "/media/big": bigListing() });
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/big" />);
    const dialog = await openBrowser(user);

    expect(
      await within(dialog).findByText(
        "Showing 500 of 2,140. Type a path to open it.",
      ),
    ).toBeVisible();
  });

  test("no cap line when every folder is shown", async () => {
    serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "Artist - Album" });

    expect(within(dialog).queryByText(/^Showing/)).not.toBeInTheDocument();
  });

  test("over the cap, the empty line says only the first ones were searched", async () => {
    serveTree({ "/media/big": bigListing() });
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/big" />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "f000" });

    await user.type(pathBox(dialog), "zzz");

    expect(
      within(dialog).getByText("None of the first 500 match “zzz”."),
    ).toBeVisible();
  });

  test("over the cap on a refused folder, the line holds the refusal", async () => {
    serveTree({ "/media/big": bigListing(LIBRARY_PARENT) });
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/big" />);
    const dialog = await openBrowser(user);

    expect(await within(dialog).findByText(LIBRARY_PARENT)).toBeVisible();
    expect(within(dialog).queryByText(/^Showing/)).not.toBeInTheDocument();
  });

  test("the line under the list stays, empty, when the refusal goes", async () => {
    // So the centred dialog keeps its height. jsdom has no layout: the
    // reserved line is the `min-h-lh` class (browser-checked at 360×560).
    serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media" />);
    const dialog = await openBrowser(user);
    const line = await within(dialog).findByText(LIBRARY_PARENT);

    await user.click(within(dialog).getByRole("button", { name: "downloads" }));
    await within(dialog).findByRole("button", { name: "Artist - Album" });

    expect(line).toBeInTheDocument();
    expect(line).toBeEmptyDOMElement();
    expect(line).toHaveClass("min-h-lh");
  });
});

describe("FolderBrowserDialog: asking the server", () => {
  test("a refusal shows after one request, not after a retry", async () => {
    // The app's own client, whose default retries once, a second later.
    const asked = serveTree();
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={createAppQueryClient()}>
        <Host initial="/media/downloads/Locked" />
      </QueryClientProvider>,
    );
    const dialog = await openBrowser(user);

    expect(
      await within(dialog).findByRole("alert", {}, { timeout: 3000 }),
    ).toHaveTextContent(UNREADABLE);
    expect(asked).toEqual(["/media/downloads/Locked"]);
  });

  test("closing the dialog stops the request it was waiting on", async () => {
    let signal: AbortSignal | undefined;
    server.use(
      http.get(FOLDERS_URL, async ({ request }) => {
        signal = request.signal;
        await new Promise<void>((resolve) => {
          request.signal.addEventListener("abort", () => resolve());
        });
        return HttpResponse.error();
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/mnt/hung" />);
    const dialog = await openBrowser(user);
    await waitFor(() => expect(signal).toBeDefined());

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(signal?.aborted).toBe(true));
  });
});

describe("FolderBrowserDialog: failures and waiting", () => {
  test("an error keeps the last list, and Use still takes that folder", async () => {
    serveTree();
    const onUse = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" onUse={onUse} />);
    const dialog = await openBrowser(user);

    await user.click(
      await within(dialog).findByRole("button", { name: "Locked" }),
    );

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      UNREADABLE,
    );
    expect(
      within(dialog).getByRole("button", { name: "Artist - Album" }),
    ).toBeInTheDocument();

    await user.click(useButton(dialog));
    expect(onUse).toHaveBeenCalledWith("/media/downloads");
  });

  test("opening a failed folder again asks again", async () => {
    const asked = serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" />);
    const dialog = await openBrowser(user);
    const locked = await within(dialog).findByRole("button", {
      name: "Locked",
    });

    await user.click(locked);
    await within(dialog).findByRole("alert");
    await user.click(locked);

    await waitFor(() =>
      expect(
        asked.filter((p) => p === "/media/downloads/Locked"),
      ).toHaveLength(2),
    );
  });

  test("a folder that lands after the user moved to the path box leaves focus there", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    serveTree({
      "/media/downloads/Other Album": listing(
        "/media/downloads/Other Album",
        "/media/downloads",
        ["CD1"],
      ),
    });
    server.use(
      http.get(FOLDERS_URL, async ({ request }) => {
        const path = new URL(request.url).searchParams.get("path");
        if (path === "/media/downloads/Other Album") await gate;
        return undefined;
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" />);
    const dialog = await openBrowser(user);

    await user.click(
      await within(dialog).findByRole("button", { name: "Other Album" }),
    );
    await user.click(pathBox(dialog));
    release();

    await within(dialog).findByRole("button", { name: "CD1" });
    expect(pathBox(dialog)).toHaveFocus();
  });

  test("a first load that fails says why inside the box, and Use waits", async () => {
    server.use(
      http.get(FOLDERS_URL, () =>
        HttpResponse.json({ detail: UNREADABLE }, { status: 422 }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads/Locked" />);
    const dialog = await openBrowser(user);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      UNREADABLE,
    );
    expect(useButton(dialog)).toHaveAttribute("aria-disabled", "true");
  });

  test("a later load keeps the list on screen, busy, and Use waits", async () => {
    serveTree();
    server.use(
      http.get(FOLDERS_URL, async ({ request }) => {
        if (new URL(request.url).searchParams.get("path") === "/media") {
          await delay("infinite");
        }
        return undefined;
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "Artist - Album" });
    expect(useButton(dialog)).toHaveAttribute("aria-disabled", "false");

    await user.click(within(dialog).getByRole("button", { name: "Up" }));

    expect(
      within(dialog).getByRole("button", { name: "Artist - Album" }),
    ).toBeInTheDocument();
    expect(useButton(dialog)).toHaveAttribute("aria-disabled", "true");
    expect(
      within(dialog).getByRole("list").parentElement,
    ).toHaveAttribute("aria-busy", "true");
  });
});

describe("FolderBrowserDialog: using a folder", () => {
  test("Use is aria-disabled on a refused folder, with the reason read out", async () => {
    serveTree();
    const onUse = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media" onUse={onUse} />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "downloads" });

    const use = useButton(dialog);
    expect(use).toHaveAttribute("aria-disabled", "true");
    expect(use).toBeEnabled();
    expect(use).toHaveAccessibleDescription(LIBRARY_PARENT);

    await user.click(use);

    expect(onUse).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  test("Use waits while the box names a folder that failed", async () => {
    // The list on screen is the last good folder; Use would send that one
    // while the box shows another.
    serveTree();
    const onUse = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" onUse={onUse} />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "Locked" });

    await user.type(pathBox(dialog), "Locked/");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      UNREADABLE,
    );
    expect(pathBox(dialog)).toHaveValue("/media/downloads/Locked/");
    expect(useButton(dialog)).toHaveAttribute("aria-disabled", "true");
    await user.click(useButton(dialog));

    expect(onUse).not.toHaveBeenCalled();
  });

  test("Use hands the listed folder to the field's setter and focuses the field", async () => {
    serveTree();
    const onUse = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" onUse={onUse} />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "Artist - Album" });

    const use = useButton(dialog);
    expect(use).toHaveAttribute("aria-disabled", "false");
    expect(use).not.toHaveAttribute("aria-describedby");
    await user.click(use);

    expect(onUse).toHaveBeenCalledWith("/media/downloads");
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    const field = screen.getByRole("textbox", { name: "Field" });
    await waitFor(() => expect(field).toHaveFocus());
    expect(field).toHaveValue("/media/downloads");
  });

  test("Cancel returns focus to the trigger and changes nothing", async () => {
    serveTree();
    const onUse = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" onUse={onUse} />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "Artist - Album" });

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    const trigger = screen.getByRole("button", { name: "Browse folders" });
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(onUse).not.toHaveBeenCalled();
  });

  test("Escape returns focus to the trigger", async () => {
    serveTree();
    const user = userEvent.setup();
    renderWithProviders(<Host initial="/media/downloads" />);
    const dialog = await openBrowser(user);
    await within(dialog).findByRole("button", { name: "Artist - Album" });

    await user.keyboard("{Escape}");

    const trigger = screen.getByRole("button", { name: "Browse folders" });
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  test("reopening, with the listing cached, focuses the first row again", async () => {
    // The app's client keeps the listing fresh, so it lands on the first
    // render, while the trigger still has focus and before Radix moves it in.
    serveTree();
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={createAppQueryClient()}>
        <Host initial="/media/downloads" />
      </QueryClientProvider>,
    );
    let dialog = await openBrowser(user);
    let first = await within(dialog).findByRole("button", {
      name: "Artist - Album",
    });
    await waitFor(() => expect(first).toHaveFocus());
    await user.click(useButton(dialog));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );

    dialog = await openBrowser(user);

    first = within(dialog).getByRole("button", { name: "Artist - Album" });
    await waitFor(() => expect(first).toHaveFocus());
  });
});
