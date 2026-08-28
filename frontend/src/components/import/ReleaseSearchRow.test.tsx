import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ReleaseSearchRow } from "@/components/import/ReleaseSearchRow";

function renderRow(over: Partial<React.ComponentProps<typeof ReleaseSearchRow>> = {}) {
  const onSearch = vi.fn();
  render(
    <ReleaseSearchRow
      onSearch={onSearch}
      busy={false}
      feedback={null}
      error={false}
      formId="search-row"
      {...over}
    />,
  );
  return onSearch;
}

test("a URL wins over filled artist/album fields", async () => {
  const onSearch = renderRow();
  await userEvent.type(
    screen.getByLabelText(/release url or id/i),
    "https://musicbrainz.org/release/x",
  );
  await userEvent.type(screen.getByLabelText(/^artist$/i), "2 Brothers");
  await userEvent.type(screen.getByLabelText(/^album$/i), "Dreams");
  await userEvent.click(screen.getByRole("button", { name: /^search$/i }));
  expect(onSearch).toHaveBeenCalledWith({
    release_id: "https://musicbrainz.org/release/x",
    artist: null,
    album: null,
    force_non_va: true,
  });
});

test("a name search posts artist+album and honors the compilation checkbox", async () => {
  const onSearch = renderRow();
  expect(screen.getByRole("checkbox", { name: /not a compilation/i })).toBeChecked();
  await userEvent.click(screen.getByRole("checkbox", { name: /not a compilation/i }));
  await userEvent.type(screen.getByLabelText(/^artist$/i), "2 Brothers");
  await userEvent.type(screen.getByLabelText(/^album$/i), "Dreams");
  await userEvent.click(screen.getByRole("button", { name: /^search$/i }));
  expect(onSearch).toHaveBeenCalledWith({
    release_id: null,
    artist: "2 Brothers",
    album: "Dreams",
    force_non_va: false,
  });
});

test("Search stays disabled until a URL or BOTH name fields exist", async () => {
  renderRow();
  expect(screen.getByRole("button", { name: /^search$/i })).toBeDisabled();
  await userEvent.type(screen.getByLabelText(/^artist$/i), "2 Brothers");
  expect(screen.getByRole("button", { name: /^search$/i })).toBeDisabled();
  await userEvent.type(screen.getByLabelText(/^album$/i), "Dreams");
  expect(screen.getByRole("button", { name: /^search$/i })).toBeEnabled();
});

test("the URL hint lives in the placeholder, not a paragraph", () => {
  renderRow();
  expect(
    screen.getByPlaceholderText("MusicBrainz/Deezer release URL or ID"),
  ).toBeInTheDocument();
  // ("paste a musicbrainz release" never existed as paragraph copy in this component — placeholder only — so no absence pin.)
});

test("feedback and error lines render with live-region roles", () => {
  renderRow({ feedback: "No release found. Showing your previous matches.", error: true });
  expect(screen.getByRole("status")).toHaveTextContent(/no release found/i);
  expect(screen.getByRole("alert")).toHaveTextContent(/couldn’t run that search/i);
});

test("the Various Artists escape hatch lives in the info popover", async () => {
  renderRow();
  await userEvent.click(screen.getByRole("button", { name: /why paste a url/i }));
  expect(
    await screen.findByText(/keeps matching a Various Artists compilation/i),
  ).toBeInTheDocument();
});

test("busy disables every field and the submit", () => {
  renderRow({ busy: true });
  expect(screen.getByLabelText(/release url or id/i)).toBeDisabled();
  expect(screen.getByLabelText(/^artist$/i)).toBeDisabled();
  expect(screen.getByLabelText(/^album$/i)).toBeDisabled();
  expect(screen.getByRole("button", { name: /searching/i })).toBeDisabled();
});
