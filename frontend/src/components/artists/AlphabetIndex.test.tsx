import { screen } from "@testing-library/react";
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
