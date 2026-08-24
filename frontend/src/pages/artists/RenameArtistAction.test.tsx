import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { client } from "@/api/client";
import type { components } from "@/api/schema";
import { RenameArtistAction } from "@/pages/artists/RenameArtistAction";

function ok(data: unknown) {
  return { data, error: undefined, response: { ok: true, status: 200 } } as never;
}

const PREVIEW: components["schemas"]["ArtistRenamePreview"] = {
  name: "Fayrouz",
  new_name: "Fairuz",
  move_enabled: true,
  albums: [
    { album_id: 1, title: "Best Of", move_count: 2, refusals: [] },
    { album_id: 2, title: "Live", move_count: 1, refusals: [] },
  ],
  merge: { existing_album_count: 1 },
};

const RESULT: components["schemas"]["ArtistRenameResult"] = {
  name: "Fayrouz",
  new_name: "Fairuz",
  albums: [
    { album_id: 1, title: "Best Of", outcome: "renamed", write_failures: 0, move_failures: 0, error: null },
    { album_id: 2, title: "Live", outcome: "renamed", write_failures: 0, move_failures: 0, error: null },
  ],
  old_name_remaining_albums: 0,
  portrait: "moved",
  playlists_reexported: 1,
  artist_art_job: "not_needed",
};

function Loc() {
  return <div data-testid="loc">{useLocation().pathname}</div>;
}

function renderAction() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/artists/Fayrouz"]}>
        <Routes>
          <Route
            path="/artists/:name"
            element={
              <>
                <RenameArtistAction name="Fayrouz" />
                <Loc />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Open the dialog, set the new name, run a successful preview. */
async function openAndPreview() {
  await userEvent.click(screen.getByRole("button", { name: /rename artist/i }));
  const input = screen.getByLabelText(/new name/i);
  await userEvent.clear(input);
  await userEvent.type(input, "Fairuz");
  await userEvent.click(screen.getByRole("button", { name: /preview/i }));
  await screen.findByText(/best of/i);
}

describe("RenameArtistAction", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("gates the apply verb on a fresh preview and re-gates on input change", async () => {
    const post = vi.spyOn(client, "POST").mockResolvedValue(ok(PREVIEW));
    renderAction();

    await userEvent.click(screen.getByRole("button", { name: /rename artist/i }));
    const input = screen.getByLabelText(/new name/i);
    // Pre-preview the verb is "Apply"; a merge preview flips it to "Merge".
    expect(screen.getByRole("button", { name: /^apply/i })).toHaveAttribute(
      "aria-disabled",
      "true",
    );

    await userEvent.clear(input);
    await userEvent.type(input, "Fairuz");
    await userEvent.click(screen.getByRole("button", { name: /preview/i }));

    // Preview rendered: albums, the merge consequence, and the warning banner.
    expect(await screen.findByText(/best of/i)).toBeInTheDocument();
    expect(screen.getByText(/merges the two artists into one/i)).toBeInTheDocument();
    expect(
      screen.getByText(/will be moved on disk; this relocates the files/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^merge/i })).not.toHaveAttribute(
      "aria-disabled",
    );
    expect(post).toHaveBeenCalledWith("/api/artists/rename/preview", {
      body: { name: "Fayrouz", new_name: "Fairuz" },
    });

    // Any keystroke invalidates the preview and re-gates the apply verb
    // (which flips back to "Apply" now that the merge preview is gone).
    await userEvent.type(input, "!");
    expect(screen.getByRole("button", { name: /^apply/i })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("applies after preview and navigates to the renamed artist", async () => {
    vi.spyOn(client, "POST").mockImplementation(async (url: string) =>
      url.endsWith("/preview") ? ok(PREVIEW) : ok(RESULT),
    );
    renderAction();

    await openAndPreview();
    await userEvent.click(screen.getByRole("button", { name: /^merge/i }));

    await waitFor(() =>
      expect(screen.getByTestId("loc")).toHaveTextContent("/artists/Fairuz"),
    );
  });

  it("shows per-album failures instead of navigating", async () => {
    const first = RESULT.albums[0]!;
    const second = RESULT.albums[1]!;
    const partial: components["schemas"]["ArtistRenameResult"] = {
      ...RESULT,
      albums: [first, { ...second, outcome: "failed", error: "disk on fire" }],
      old_name_remaining_albums: 1,
      portrait: "not_rekeyed",
    };
    vi.spyOn(client, "POST").mockImplementation(async (url: string) =>
      url.endsWith("/preview") ? ok(PREVIEW) : ok(partial),
    );
    renderAction();

    await openAndPreview();
    await userEvent.click(screen.getByRole("button", { name: /^merge/i }));

    expect(await screen.findByText(/disk on fire/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /go to renamed artist/i })).toBeInTheDocument();
    expect(screen.getByTestId("loc")).toHaveTextContent("/artists/Fayrouz");
  });

  it("swallows a click on the gated apply verb (endpoint not called)", async () => {
    const post = vi.spyOn(client, "POST").mockResolvedValue(ok(PREVIEW));
    renderAction();

    await userEvent.click(screen.getByRole("button", { name: /rename artist/i }));
    const apply = screen.getByRole("button", { name: /^apply/i });
    expect(apply).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(apply);

    expect(post).not.toHaveBeenCalled();
  });

  it("does not navigate when a renamed album has write/move failures", async () => {
    const damaged: components["schemas"]["ArtistRenameResult"] = {
      ...RESULT,
      albums: [
        { ...RESULT.albums[0]!, write_failures: 3, move_failures: 2 },
        RESULT.albums[1]!,
      ],
      old_name_remaining_albums: 1,
    };
    vi.spyOn(client, "POST").mockImplementation(async (url: string) =>
      url.endsWith("/preview") ? ok(PREVIEW) : ok(damaged),
    );
    renderAction();

    await openAndPreview();
    await userEvent.click(screen.getByRole("button", { name: /^merge/i }));

    expect(
      await screen.findByText(/3 files failed to write and 2 failed to move/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /go to renamed artist/i })).toBeInTheDocument();
    expect(screen.getByTestId("loc")).toHaveTextContent("/artists/Fayrouz");
  });

  it("maps a 409 apply to the library-job message", async () => {
    vi.spyOn(client, "POST").mockImplementation(async (url: string) =>
      url.endsWith("/preview")
        ? ok(PREVIEW)
        : ({
            data: undefined,
            error: {},
            response: { ok: false, status: 409 },
          } as never),
    );
    renderAction();

    await openAndPreview();
    await userEvent.click(screen.getByRole("button", { name: /^merge/i }));

    expect(
      await screen.findByText(/a library job is running; try again when it finishes/i),
    ).toBeInTheDocument();
    expect(screen.getByTestId("loc")).toHaveTextContent("/artists/Fayrouz");
  });
});
