import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AlphabetIndex } from "@/components/artists/AlphabetIndex";
import { renderWithProviders } from "@/test/render";

const roster = (names: string[]) =>
  names.map((name) => ({ name, album_count: 1 }));

describe("AlphabetIndex", () => {
  it("jumps to the page containing the first artist of a letter", async () => {
    const onJump = vi.fn();
    // 60 artists starting with A, then B — "B" lands on page 2 (offset 48).
    const names = [
      ...Array.from(
        { length: 60 },
        (_, i) => `A-artist ${String(i).padStart(2, "0")}`,
      ),
      "Bee Gees",
    ];
    renderWithProviders(
      <AlphabetIndex
        artists={roster(names)}
        pageSize={48}
        offset={0}
        onJump={onJump}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Jump to artists starting with B" }),
    );
    expect(onJump).toHaveBeenCalledWith(48);
  });

  it("buckets diacritics with their base letter and leading symbols/digits as #", () => {
    renderWithProviders(
      <AlphabetIndex
        artists={roster(["*NSYNC", "Édith Piaf", "10cc"])}
        pageSize={48}
        offset={0}
        onJump={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: /starting with #/ }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: /starting with E/ }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: /starting with A/ }),
    ).toBeDisabled();
  });

  it("marks the letters visible on the current page", () => {
    renderWithProviders(
      <AlphabetIndex
        artists={roster(["ABBA", "Beck", "Cher"])}
        pageSize={2}
        offset={0}
        onJump={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: /starting with A/ }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByRole("button", { name: /starting with C/ }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("keeps a letter pressed on every page its bucket spans, not just the page holding its first artist", () => {
    // 50 artists starting with "A", then one "Bee Gees" — pageSize 48 means
    // "A" artists occupy BOTH page 1 (indices 0-47) and page 2 (48-49). "A"
    // must read as pressed on page 2 as well as page 1, because the pressed
    // set is derived from what's actually on screen, not just each bucket's
    // first occurrence.
    const names = [
      ...Array.from(
        { length: 50 },
        (_, i) => `A-artist ${String(i).padStart(2, "0")}`,
      ),
      "Bee Gees",
    ];
    renderWithProviders(
      <AlphabetIndex
        artists={roster(names)}
        pageSize={48}
        offset={48}
        onJump={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: /starting with A/ }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.getByRole("button", { name: /starting with B/ }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("wraps rather than clipping, so no bucket is ever off-screen", () => {
    renderWithProviders(
      <AlphabetIndex
        artists={roster(["ABBA"])}
        pageSize={48}
        offset={0}
        onJump={vi.fn()}
      />,
    );
    const nav = screen.getByRole("navigation", {
      name: "Jump to artists by letter",
    });
    const toolbar = within(nav).getByRole("toolbar");
    // This is a TARGETING control: you aim at a remembered position, and the
    // pressed tint is the only "where am I" signal — on the last page that
    // signal is V-Z, i.e. exactly what a scroll box would hide. So it grows
    // taller instead of hiding its tail, and never scrolls or clips. The
    // buttons wrap inside the toolbar; the landmark is just the band's item.
    expect(toolbar.classList.contains("flex-wrap")).toBe(true);
    expect(toolbar.classList.contains("overflow-x-auto")).toBe(false);
    expect(nav.classList.contains("overflow-x-auto")).toBe(false);
    // `mr-auto` is what keeps the letters on the left of the band (and the
    // pager on the right) without the strip stretching its buttons.
    expect(nav.classList.contains("mr-auto")).toBe(true);
    // "#" plus A-Z, none of them dropped at any width.
    expect(screen.getAllByRole("button")).toHaveLength(27);
  });

  it("announces itself as a composite toolbar INSIDE the landmark", () => {
    // Two properties, and they need two elements. The landmark is how an AT
    // user reaches a jump strip at all, so it stays on the <nav>. But a
    // roving tabindex with nothing announcing it leaves that same user on one
    // tabbable button with no signal that ArrowRight reaches the other 26 —
    // they can reasonably conclude the strip offers a single letter. The
    // composite role is what carries "arrows move within", so it goes on an
    // inner container: put it on the <nav> and it would REPLACE the landmark.
    renderWithProviders(
      <AlphabetIndex
        artists={roster(["ABBA", "Cher"])}
        pageSize={48}
        offset={0}
        onJump={vi.fn()}
      />,
    );
    const nav = screen.getByRole("navigation", {
      name: "Jump to artists by letter",
    });
    const toolbar = within(nav).getByRole("toolbar");
    expect(toolbar).toHaveAttribute("aria-orientation", "horizontal");
    // Every bucket lives inside the composite, not beside it.
    expect(within(toolbar).getAllByRole("button")).toHaveLength(27);
    // Deliberately unnamed: the landmark right outside it already carries
    // that name, and repeating it makes AT read "jump to artists by letter,
    // navigation, jump to artists by letter, toolbar" on the way in.
    expect(toolbar).not.toHaveAttribute("aria-label");
    expect(toolbar).not.toHaveAttribute("aria-labelledby");
  });

  it("is ONE tab stop, anchored on the page you are looking at", () => {
    // 27 buttons ahead of the pager would cost ~29 Tab presses to reach
    // "Next page", so the strip roves: exactly one button is tabbable and the
    // arrows move within.
    const { rerender } = renderWithProviders(
      <AlphabetIndex
        artists={roster(["ABBA", "Beck", "Cher"])}
        pageSize={2}
        offset={0}
        onJump={vi.fn()}
      />,
    );
    const tabbable = () =>
      screen.getAllByRole("button").filter((b) => b.tabIndex === 0);
    expect(tabbable()).toHaveLength(1);
    // Page 1 shows ABBA + Beck, so the tab stop is "A" — Tab lands you where
    // you already are, not at the far end of the alphabet.
    expect(tabbable()[0]).toHaveAccessibleName(
      "Jump to artists starting with A",
    );

    // Page 2 shows only Cher: the anchor follows.
    rerender(
      <AlphabetIndex
        artists={roster(["ABBA", "Beck", "Cher"])}
        pageSize={2}
        offset={2}
        onJump={vi.fn()}
      />,
    );
    expect(tabbable()).toHaveLength(1);
    expect(tabbable()[0]).toHaveAccessibleName(
      "Jump to artists starting with C",
    );
  });

  it("moves between buckets with the arrows, skipping the empty ones", async () => {
    // Only A and C have artists — B is disabled, so it can't take focus and
    // the arrows must step over it rather than dead-ending on it.
    renderWithProviders(
      <AlphabetIndex
        artists={roster(["ABBA", "Cher"])}
        pageSize={48}
        offset={0}
        onJump={vi.fn()}
      />,
    );
    const a = screen.getByRole("button", { name: /starting with A/ });
    const c = screen.getByRole("button", { name: /starting with C/ });
    const hash = screen.getByRole("button", { name: /starting with #/ });
    await act(async () => a.focus());

    await userEvent.keyboard("{ArrowRight}");
    expect(c).toHaveFocus();
    expect(c).toHaveAttribute("tabindex", "0"); // the tab stop follows focus
    expect(a).toHaveAttribute("tabindex", "-1");

    await userEvent.keyboard("{ArrowLeft}");
    expect(a).toHaveFocus();

    await userEvent.keyboard("{End}");
    expect(c).toHaveFocus(); // last bucket WITH artists, not "Z"
    await userEvent.keyboard("{Home}");
    expect(a).toHaveFocus(); // first bucket with artists, not "#"
    expect(hash).toBeDisabled();
  });

  it("jumps to a diacritic artist's true run once the roster is diacritic-sorted", async () => {
    // Mirrors the FIXED backend sort (normalize_artist_name primary key):
    // "Édith Piaf" now sits between the "D" and "F" artists, not after "Z".
    // Clicking "E" must land on her true position in this pre-sorted array
    // (index 1 → page 2 at pageSize 1), not wherever she'd fall if she were
    // still sorted after "Z" by raw codepoint.
    const onJump = vi.fn();
    const names = ["Duran Duran", "Édith Piaf", "Fleetwood Mac"];
    renderWithProviders(
      <AlphabetIndex
        artists={roster(names)}
        pageSize={1}
        offset={0}
        onJump={onJump}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: /starting with E/ }),
    );
    // "Édith Piaf" is at index 1 → floor(1 / 1) * 1 = 1.
    expect(onJump).toHaveBeenCalledWith(1);
  });
});
