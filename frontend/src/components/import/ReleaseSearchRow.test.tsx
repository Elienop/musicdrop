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
  // The compilation toggle is a field too — it feeds the search that is
  // already in flight.
  expect(screen.getByRole("checkbox", { name: /not a compilation/i })).toBeDisabled();
});

test("busy swallows a click on the compilation toggle", async () => {
  renderRow({ busy: true });
  const box = screen.getByRole("checkbox", { name: /not a compilation/i });
  await userEvent.click(box);
  // The attribute is the posture; this is the behaviour behind it.
  expect(box).toHaveAttribute("aria-checked", "true");
  // The words dim with the box. The primitive's root carries `peer` and dims
  // itself at 50%; without shadcn's own Label recipe on the sibling, the label
  // stayed at full opacity over a half-faded control. jsdom has no layout
  // engine, so the classes are what a test can hold.
  expect(screen.getByText("Not a compilation")).toHaveClass(
    "peer-disabled:cursor-not-allowed",
    "peer-disabled:opacity-50",
  );
  expect(box).toHaveClass("peer");
});

// The three things the swap from a native <input type="checkbox"> had to keep.
// Each is a separate assertion because they come from different places: the
// role and the checked state from the primitive, the name and the click target
// from the sibling <label htmlFor>.
test("the compilation toggle: one name, label clicks, Space toggles", async () => {
  renderRow();
  const box = screen.getByRole("checkbox", { name: "Not a compilation" });
  // Exactly one accessible name — the words are not also read as a second
  // label, which is what a wrapping <label> plus an aria-label would give.
  expect(screen.getAllByText("Not a compilation")).toHaveLength(1);
  expect(box).toBeChecked();

  // People click the words. A dead label is worse than none.
  await userEvent.click(screen.getByText("Not a compilation"));
  expect(box).not.toBeChecked();

  // One tab stop, reached with Tab and toggled with Space.
  box.focus();
  expect(box).toHaveFocus();
  await userEvent.keyboard("{ }");
  expect(box).toBeChecked();
});
