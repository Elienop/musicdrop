import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { client } from "@/api/client";
import { RenameArtistAction } from "@/pages/artists/RenameArtistAction";

function ok(data: unknown) {
  return { data, error: undefined, response: { ok: true, status: 200 } } as never;
}

const PREVIEW = {
  name: "Fayrouz",
  new_name: "Fairuz",
  move_enabled: true,
  albums: [
    { album_id: 1, title: "Best Of", move_count: 2, refusals: [] },
    { album_id: 2, title: "Live", move_count: 1, refusals: [] },
  ],
  merge: { existing_album_count: 1 },
};

const RESULT = {
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

describe("RenameArtistAction", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("gates Apply on a fresh preview and re-gates on input change", async () => {
    const post = vi.spyOn(client, "POST").mockResolvedValue(ok(PREVIEW));
    renderAction();

    await userEvent.click(screen.getByRole("button", { name: /rename artist/i }));
    const input = screen.getByLabelText(/new name/i);
    expect(screen.getByRole("button", { name: /^apply/i })).toBeDisabled();

    await userEvent.clear(input);
    await userEvent.type(input, "Fairuz");
    await userEvent.click(screen.getByRole("button", { name: /preview/i }));

    // Preview rendered: albums + the merge note.
    expect(await screen.findByText(/best of/i)).toBeInTheDocument();
    expect(screen.getByText(/merges into/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^apply/i })).toBeEnabled();
    expect(post).toHaveBeenCalledWith("/api/artists/rename/preview", {
      body: { name: "Fayrouz", new_name: "Fairuz" },
    });

    // Any keystroke invalidates the preview and re-gates Apply.
    await userEvent.type(input, "!");
    expect(screen.getByRole("button", { name: /^apply/i })).toBeDisabled();
  });

  it("applies after preview and navigates to the renamed artist", async () => {
    vi.spyOn(client, "POST").mockImplementation(async (url: string) =>
      url.endsWith("/preview") ? ok(PREVIEW) : ok(RESULT),
    );
    renderAction();

    await userEvent.click(screen.getByRole("button", { name: /rename artist/i }));
    const input = screen.getByLabelText(/new name/i);
    await userEvent.clear(input);
    await userEvent.type(input, "Fairuz");
    await userEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByText(/best of/i);
    await userEvent.click(screen.getByRole("button", { name: /^apply/i }));

    await waitFor(() =>
      expect(screen.getByTestId("loc")).toHaveTextContent("/artists/Fairuz"),
    );
  });

  it("shows per-album failures instead of navigating", async () => {
    const first = RESULT.albums[0]!;
    const second = RESULT.albums[1]!;
    const partial = {
      ...RESULT,
      albums: [first, { ...second, outcome: "failed", error: "disk on fire" }],
      old_name_remaining_albums: 1,
      portrait: "not_rekeyed",
    };
    vi.spyOn(client, "POST").mockImplementation(async (url: string) =>
      url.endsWith("/preview") ? ok(PREVIEW) : ok(partial),
    );
    renderAction();

    await userEvent.click(screen.getByRole("button", { name: /rename artist/i }));
    const input = screen.getByLabelText(/new name/i);
    await userEvent.clear(input);
    await userEvent.type(input, "Fairuz");
    await userEvent.click(screen.getByRole("button", { name: /preview/i }));
    await screen.findByText(/best of/i);
    await userEvent.click(screen.getByRole("button", { name: /^apply/i }));

    expect(await screen.findByText(/disk on fire/i)).toBeInTheDocument();
    expect(screen.getByTestId("loc")).toHaveTextContent("/artists/Fayrouz");
  });
});
