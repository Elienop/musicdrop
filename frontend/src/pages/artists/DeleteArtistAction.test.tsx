import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { client } from "@/api/client";
import { DeleteArtistAction } from "@/pages/artists/DeleteArtistAction";

function ok(data: unknown) {
  return { data, error: undefined, response: { ok: true, status: 200 } } as never;
}

function Loc() {
  return <div data-testid="loc">{useLocation().pathname}</div>;
}

function renderAction() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/artists/Daft%20Punk"]}>
        <Routes>
          <Route
            path="/artists/:name"
            element={
              <>
                <DeleteArtistAction name="Daft Punk" albumCount={3} />
                <Loc />
              </>
            }
          />
          <Route path="/artists" element={<Loc />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("DeleteArtistAction", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("confirms, deletes by name (query param), then navigates to the roster", async () => {
    const del = vi
      .spyOn(client, "DELETE")
      .mockResolvedValue(ok({ trashed_albums: 3, trash_path: "/trash" }));
    renderAction();

    await userEvent.click(screen.getByRole("button", { name: /delete artist/i }));
    // Count + name surface in the confirm copy.
    expect(await screen.findByText(/all 3 albums by/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /move to trash/i }));

    await waitFor(() =>
      expect(del).toHaveBeenCalledWith("/api/artists", {
        params: { query: { name: "Daft Punk" } },
      }),
    );
    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent(/^\/artists$/));
  });

  it("reopens with no alert from a failed delete", async () => {
    vi.spyOn(client, "DELETE").mockResolvedValue({
      data: undefined,
      error: { detail: "A library operation is in progress" },
      response: { ok: false, status: 409 },
    } as never);
    renderAction();

    const btn = screen.getByRole("button", { name: /delete artist/i });
    await userEvent.click(btn);
    await userEvent.click(screen.getByRole("button", { name: /move to trash/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/library operation is in progress/i);

    // The mutation outlives the dialog, so the next open must not inherit it.
    await userEvent.click(screen.getByRole("button", { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await userEvent.click(btn);
    await screen.findByRole("alertdialog");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
