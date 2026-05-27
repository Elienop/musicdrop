import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import type { Candidate } from "@/api/useImport";
import { ImportCandidatePage } from "@/pages/import/ImportCandidatePage";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

const CANDIDATE_URL = `${window.location.origin}/api/import/job-1/albums/1`;
const CHOICE_URL = `${window.location.origin}/api/import/job-1/albums/1/choice`;

function makeCandidate(overrides: Partial<Candidate> = {}): Candidate {
  return {
    confidence: 76,
    recommendation: "medium",
    data_source: "MusicBrainz",
    data_url: "https://musicbrainz.org/release/abc",
    cover_after_url: "https://coverartarchive.org/release/abc/front-500",
    has_current_art: false,
    changed_fields: ["album", "label"],
    album_before: {
      artist: "Radiohead",
      album: "OK Computr",
      year: null,
      label: null,
      country: null,
      media: null,
    },
    album_after: {
      artist: "Radiohead",
      album: "OK Computer",
      year: 1997,
      label: "Parlophone",
      country: "GB",
      media: "CD",
    },
    tracks: [
      {
        index: 1,
        status: "unchanged",
        title_before: "Airbag",
        title_after: "Airbag",
        track_before: 1,
        track_after: 1,
      },
      {
        index: 2,
        status: "changed",
        title_before: "Paranoid Andrid",
        title_after: "Paranoid Android",
        track_before: 2,
        track_after: 2,
      },
    ],
    missing: [{ index: 10, title: "Lull" }],
    unmatched: [{ title: "bonus.mp3", track: null }],
    options: [
      {
        index: 0,
        confidence: 76,
        data_source: "MusicBrainz",
        disambiguation: "1997 UK CD",
      },
      {
        index: 1,
        confidence: 71,
        data_source: "MusicBrainz",
        disambiguation: "2008 reissue",
      },
    ],
    ...overrides,
  };
}

function renderAt(route = "/import/albums/1?job=job-1") {
  return renderWithProviders(<ImportCandidatePage />, {
    route,
    path: "/import/albums/:index",
  });
}

describe("ImportCandidatePage", () => {
  test("renders header, source, what-changes, before/after and the tracklist", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    expect(
      await screen.findByRole("heading", { name: /Radiohead — OK Computer/i }),
    ).toBeInTheDocument();
    // "Medium match" is unique to the header (the switcher options carry % +
    // source, not the recommendation label).
    expect(screen.getByText(/Medium match/i)).toBeInTheDocument();
    // The confidence sits in its own header <span> ("76%"); a bare /76%/ would
    // also match the switcher's first <option>, so pin it to the SPAN.
    expect(
      screen.getByText(
        (_, el) => el?.tagName === "SPAN" && el.textContent === "76%",
      ),
    ).toBeInTheDocument();
    // what-changes chips
    expect(screen.getByText("+ cover art")).toBeInTheDocument();
    expect(screen.getByText("1 of 2 titles")).toBeInTheDocument();
    // before vs after album title both present
    expect(screen.getByText("OK Computr")).toBeInTheDocument();
    expect(screen.getByText("OK Computer")).toBeInTheDocument();
    // tracklist incl. missing + unmatched
    expect(screen.getByText("Paranoid Android")).toBeInTheDocument();
    expect(screen.getByText("Lull")).toBeInTheDocument();
    expect(screen.getByText("bonus.mp3")).toBeInTheDocument();
  });

  test("shows the missing + not-on-release caveat chips", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    // The Changes row carries caveat chips distinct from the tracklist rows:
    // one for the missing track, one for the unmatched file.
    expect(await screen.findByText("1 missing")).toBeInTheDocument();
    expect(screen.getByText("1 not on release")).toBeInTheDocument();
  });

  test("the action buttons carry title hints + a helper line under the bar", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    renderAt();

    expect(
      await screen.findByRole("button", { name: /use as-is/i }),
    ).toHaveAttribute(
      "title",
      "Import with the current tags, without a MusicBrainz match",
    );
    expect(screen.getByRole("button", { name: /as tracks/i })).toHaveAttribute(
      "title",
      "Import each file as a standalone track, not grouped as an album",
    );
    expect(
      screen.getByText(
        /Use as-is keeps your current tags · As tracks imports files individually\./,
      ),
    ).toBeInTheDocument();
  });

  test("Apply posts apply + the top candidate index, then returns to the feed", async () => {
    let body: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await user.click(await screen.findByRole("button", { name: /^Apply/i }));
    await waitFor(() =>
      expect(body).toEqual({ action: "apply", candidate_index: 0 }),
    );
  });

  test("selecting an alternate shows the switcher note (absent at top match)", async () => {
    server.use(http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())));
    const user = userEvent.setup();
    renderAt();

    // At the default top match (selected === 0) the note is absent.
    await screen.findByLabelText(/candidate release/i);
    expect(
      screen.queryByText(/Apply will import the selected release/i),
    ).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText(/candidate release/i), "1");

    const note = await screen.findByText(
      /Showing the top match — Apply will import the selected release\./,
    );
    expect(note).toBeInTheDocument();
    expect(note).toHaveAttribute("role", "status");
  });

  test("switching candidate then Apply posts the chosen index", async () => {
    let body: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await user.selectOptions(
      await screen.findByLabelText(/candidate release/i),
      "1",
    );
    await user.click(screen.getByRole("button", { name: /^Apply/i }));
    await waitFor(() =>
      expect(body).toEqual({ action: "apply", candidate_index: 1 }),
    );
  });

  test("Skip posts skip (no candidate index)", async () => {
    let body: unknown = null;
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderAt();

    await user.click(await screen.findByRole("button", { name: /^Skip/i }));
    await waitFor(() =>
      expect(body).toEqual({ action: "skip", candidate_index: null }),
    );
  });

  test("a 500 on submit surfaces an inline error and does not navigate", async () => {
    server.use(
      http.get(CANDIDATE_URL, () => HttpResponse.json(makeCandidate())),
      http.post(CHOICE_URL, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    renderAt();

    await user.click(await screen.findByRole("button", { name: /^Apply/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Couldn’t submit that choice — try again\./);
    // Did NOT navigate away — still on the review screen.
    expect(
      screen.getByRole("heading", { name: /Radiohead — OK Computer/i }),
    ).toBeInTheDocument();
  });

  test("a 404 (no longer parked) shows the calm 'not waiting' notice", async () => {
    server.use(
      http.get(CANDIDATE_URL, () =>
        HttpResponse.json({ detail: "Import album not found" }, { status: 404 }),
      ),
    );
    renderAt();
    expect(
      await screen.findByText(/isn.t waiting for review/i),
    ).toBeInTheDocument();
  });

  test("a missing job id sends the user back with a notice", async () => {
    // No ?job= -> nothing to fetch (the GET handler is never hit).
    renderAt("/import/albums/1");
    expect(screen.getByText(/nothing to review/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Import" })).toHaveAttribute(
      "href",
      "/import",
    );
  });
});
