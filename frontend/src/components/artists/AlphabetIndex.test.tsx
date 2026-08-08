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
});
