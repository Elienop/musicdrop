import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ArtistAlbumsPage } from "@/pages/artists/ArtistAlbumsPage";

vi.mock("@/api/useAlbums", () => ({
  useAlbums: () => ({
    data: { items: [], total: 0 },
    isPending: false,
    isError: false,
    isFetching: false,
    refetch: vi.fn(),
  }),
}));

// Artist-image edit lives behind its own toggle; keep it OFF so the only header
// button under test is "Apply to library".
vi.mock("@/api/useArtistImage", () => ({
  ARTIST_IMAGE_SETTINGS_KEY: ["artist-image", "settings"],
  useArtistImageSettings: () => ({ data: { enabled: false } }),
}));

// The reorganize header control runs a live status useQuery; stub it so this
// raw render (no QueryClientProvider) doesn't crash. Idle status + no-op
// mutations keep the control inert and out of the way of this suite.
vi.mock("@/api/useReorganize", () => ({
  useReorganizeStatus: () => ({
    data: { phase: "idle", scope: null, artist: null, album_id: null },
  }),
  usePreviewReorganize: () => ({ mutate: vi.fn(), isPending: false }),
  useStartReorganize: () => ({ mutate: vi.fn(), isPending: false }),
  useStopReorganize: () => ({ mutate: vi.fn(), isPending: false }),
  useDismissReorganize: () => ({ mutate: vi.fn(), isPending: false }),
}));

const writeSettings = { enabled: true };
/** Stands in for the apply hook's `mutationFn`, so a test can make the start
 * hang (Escape guard) or fail (the stale-alert reset). */
const applyMutate = vi.fn<() => Promise<unknown>>(() => Promise.resolve({}));
const backfillStatus: {
  phase: string;
  artist: string | null;
  processed: number;
  total: number;
  written: number;
  skipped: number;
  failed: number;
} = {
  phase: "idle",
  artist: null,
  processed: 0,
  total: 0,
  written: 0,
  skipped: 0,
  failed: 0,
};

// The apply hook is mocked over a REAL useMutation: a frozen object mock has
// no subscription, so `reset()` could not be observed and the in-flight window
// (isPending) could never be entered from an open dialog.
vi.mock("@/api/useArtistArt", async () => {
  const { useMutation } = await import("@tanstack/react-query");
  return {
    useArtistArtSettings: () => ({ data: writeSettings }),
    useArtistArtBackfillStatus: () => ({ data: backfillStatus }),
    useStartArtistArtApply: () =>
      useMutation<unknown, Error, void>({ mutationFn: applyMutate }),
  };
});

function renderAt(name: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/artists/${name}`]}>
        <Routes>
          <Route path="/artists/:artistName" element={<ArtistAlbumsPage />} />
          <Route path="/" element={<div>home</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  writeSettings.enabled = true;
  applyMutate.mockReset();
  applyMutate.mockImplementation(() => Promise.resolve({}));
  backfillStatus.phase = "idle";
  backfillStatus.artist = null;
  backfillStatus.processed = 0;
  backfillStatus.total = 0;
  backfillStatus.written = 0;
  backfillStatus.skipped = 0;
  backfillStatus.failed = 0;
});

describe("ArtistAlbumsPage artist-art apply", () => {
  it("confirms first, then starts the job exactly once", async () => {
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /save art to library/i });
    await userEvent.click(btn);
    // The click opens the confirm, it does NOT write: the apply replaces the
    // folder's artist-poster/artist-background and trashes what it replaces.
    expect(applyMutate).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/save art to library\?/i);
    // The second sentence binds to the two files the first one names.
    expect(dialog).toHaveTextContent(
      /artist-poster and artist-background files into this artist\u2019s folders\. Existing ones move to Trash first\./i,
    );

    await userEvent.click(screen.getByRole("button", { name: "Save art" }));
    await waitFor(() => expect(applyMutate).toHaveBeenCalledTimes(1));
  });

  it("swallows Escape while the start is in flight", async () => {
    applyMutate.mockImplementation(() => new Promise(() => {})); // never settles
    renderAt("ABBA");
    await userEvent.click(screen.getByRole("button", { name: /save art to library/i }));
    await screen.findByRole("alertdialog");
    await userEvent.click(screen.getByRole("button", { name: "Save art" }));
    // Cancel is disabled in this window, so Escape must not be a way out
    // either: the dialog is where a failed start reports.
    await screen.findByRole("button", { name: "Saving\u2026" });
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();

    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("reopens with no alert from a failed start", async () => {
    applyMutate.mockRejectedValue(new Error("A library operation is in progress"));
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /save art to library/i });
    await userEvent.click(btn);
    await userEvent.click(screen.getByRole("button", { name: "Save art" }));
    // The failure reports in the open dialog, then the user leaves.
    expect(await screen.findByRole("alert")).toHaveTextContent(/library operation is in progress/i);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());

    await userEvent.click(btn);
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/save art to library\?/i);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("starts nothing when the confirm is cancelled", async () => {
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /save art to library/i });
    await userEvent.click(btn);
    await screen.findByRole("alertdialog");

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(applyMutate).not.toHaveBeenCalled();
  });

  it("cancels on Escape and hands focus back to the trigger", async () => {
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /save art to library/i });
    await userEvent.click(btn);
    await screen.findByRole("alertdialog");

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(applyMutate).not.toHaveBeenCalled();
    // Focus must land back on the icon, not on <body>.
    expect(btn).toHaveFocus();
  });

  // Pins the WIRING, not the sentence (RenameArtistAction.test.tsx owns that):
  // the note must follow the art-write toggle, not the image toggle.
  it("carries the write toggle into the rename dialog's art note", async () => {
    renderAt("ABBA");
    await userEvent.click(screen.getByRole("button", { name: /rename artist/i }));
    expect(await screen.findByText(/artist art is written into the new folder/i)).toBeInTheDocument();
  });

  it("drops the rename dialog's art note when write is disabled", async () => {
    writeSettings.enabled = false;
    renderAt("ABBA");
    await userEvent.click(screen.getByRole("button", { name: /rename artist/i }));
    await screen.findByText(/track artists are not touched/i);
    expect(screen.queryByText(/artist art is written/i)).toBeNull();
  });

  it("hides the Save art to library button when write is disabled", () => {
    writeSettings.enabled = false;
    renderAt("ABBA");
    expect(
      screen.queryByRole("button", { name: /save art to library/i }),
    ).toBeNull();
  });

  it("keeps the button focusable but inert while a job runs (focus is never stranded)", () => {
    backfillStatus.phase = "running";
    backfillStatus.artist = "ABBA";
    backfillStatus.processed = 1;
    backfillStatus.total = 3;
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /save art to library/i });
    // aria-disabled, NOT disabled — disabling the focused button on
    // activation would drop keyboard focus to <body> for the whole job.
    expect(btn).toBeEnabled();
    expect(btn).toHaveAttribute("aria-disabled", "true");
    // Re-clicks while running are swallowed: no confirm, no write.
    fireEvent.click(btn);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(applyMutate).not.toHaveBeenCalled();
    // No inline progress — it must not duplicate the top app banner.
    expect(screen.queryByText(/1 \/ 3/)).toBeNull();
  });

  it("re-arms the button after the job finishes (no inline tally)", () => {
    backfillStatus.phase = "done";
    backfillStatus.artist = "ABBA";
    backfillStatus.processed = 1;
    backfillStatus.total = 1;
    backfillStatus.written = 1;
    renderAt("ABBA");
    const btn = screen.getByRole("button", { name: /save art to library/i });
    expect(btn).toBeEnabled();
    expect(btn).not.toHaveAttribute("aria-disabled");
    // The "N written" tally renders in the app shell (useActivity), not this tree, so asserting its absence here proved nothing.
  });
});
