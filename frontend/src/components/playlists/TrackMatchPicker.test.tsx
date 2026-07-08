import { screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { TrackMatchPicker } from "@/components/playlists/TrackMatchPicker";
import { renderWithProviders } from "@/test/render";

describe("TrackMatchPicker", () => {
  // jsdom has no layout, so lock the class contract that keeps the dialog's
  // content inside its box: DialogContent is a single-column CSS grid, and a
  // grid item's min-width defaults to its content — the nowrap (truncate)
  // result rows would push the column (and everything w-full inside it, the
  // search input included) wider than the dialog itself. min-w-0 on the
  // content wrapper is what pins the column to the dialog's width.
  test("content column can't outgrow the dialog and the list keeps a scrollbar gutter", () => {
    renderWithProviders(
      <TrackMatchPicker open onOpenChange={() => {}} onPick={() => {}} />,
    );
    const wrapper = screen.getByLabelText(/search library tracks/i).parentElement!;
    expect(wrapper.className).toContain("min-w-0");
    // The results scroller keeps a right padding so its scrollbar doesn't sit
    // on top of the Select buttons.
    const scroller = wrapper.querySelector(".overflow-y-auto");
    expect(scroller).not.toBeNull();
    expect(scroller!.className).toContain("pr-2");
  });
});
