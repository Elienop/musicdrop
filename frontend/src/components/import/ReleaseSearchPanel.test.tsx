import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ReleaseSearchPanel } from "@/components/import/ReleaseSearchPanel";

function renderPanel() {
  return render(
    <ReleaseSearchPanel onSearch={vi.fn()} busy={false} feedback={null} error={false} />,
  );
}

test("the description is one short line — the old paragraph is gone", () => {
  renderPanel();
  expect(
    screen.getByText("Paste a MusicBrainz release URL/ID or a Deezer album URL."),
  ).toBeInTheDocument();
  expect(screen.queryByText(/the reliable fix/i)).not.toBeInTheDocument();
});

test("the Various-Artists escape hatch lives in the info popover", async () => {
  renderPanel();
  await userEvent.click(screen.getByRole("button", { name: /why paste a url/i }));
  expect(
    await screen.findByText(/keeps matching a Various-Artists compilation/i),
  ).toBeInTheDocument();
});
