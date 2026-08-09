import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  PageSizeSelect,
  Pagination,
} from "@/components/system/Pagination";

/** Mimics a real caller (BrowsePage) whose onOffsetChange synchronously
 * flips `busy` true in the same state update that applies the new offset —
 * TanStack Query's isFetching flips true the instant a new query key is
 * requested, batched with the offset change React processes for the
 * commit. */
function BusyOnCommitHarness() {
  const [state, setState] = useState({ offset: 48, busy: false });
  return (
    <Pagination
      compact
      total={48 * 48}
      offset={state.offset}
      limit={48}
      busy={state.busy}
      onOffsetChange={(o) => setState({ offset: o, busy: true })}
    />
  );
}

describe("Pagination", () => {
  it("exports the one library page size", () => {
    expect(PAGE_SIZE).toBe(48);
  });

  it("shows 'Page X of Y' and disables only the out-of-bounds button", () => {
    render(
      <Pagination total={96} offset={0} limit={48} onOffsetChange={() => {}} />,
    );
    expect(
      screen.getByRole("navigation", { name: "Pagination" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });

  it("disables Next on the last page", () => {
    render(
      <Pagination
        total={96}
        offset={48}
        limit={48}
        onOffsetChange={() => {}}
      />,
    );
    expect(screen.getByText("Page 2 of 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous" })).toBeEnabled();
  });

  it("advances and rewinds by limit, clamping the rewind at 0", () => {
    const onOffsetChange = vi.fn();
    render(
      <Pagination
        total={200}
        offset={30}
        limit={48}
        onOffsetChange={onOffsetChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(onOffsetChange).toHaveBeenLastCalledWith(78);
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(onOffsetChange).toHaveBeenLastCalledWith(0);
  });

  it("marks the nav busy and swaps the count for a spinner", () => {
    const { container } = render(
      <Pagination
        total={96}
        offset={0}
        limit={48}
        onOffsetChange={() => {}}
        busy
      />,
    );
    expect(
      screen.getByRole("navigation", { name: "Pagination" }),
    ).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("Page 1 of 2")).not.toBeInTheDocument();
    expect(container.querySelector(".animate-spin")).not.toBeNull();
  });

  it("keeps in-bounds buttons enabled while busy but ignores clicks", () => {
    const onOffsetChange = vi.fn();
    render(
      <Pagination
        total={96}
        offset={0}
        limit={48}
        onOffsetChange={onOffsetChange}
        busy
      />,
    );
    const next = screen.getByRole("button", { name: "Next" });
    expect(next).toBeEnabled();
    fireEvent.click(next);
    expect(onOffsetChange).not.toHaveBeenCalled();
  });

  it("does not steal focus when busy toggles on", () => {
    const { rerender } = render(
      <Pagination total={96} offset={0} limit={48} onOffsetChange={() => {}} />,
    );
    const next = screen.getByRole("button", { name: "Next" });
    next.focus();
    expect(next).toHaveFocus();
    rerender(
      <Pagination
        total={96}
        offset={0}
        limit={48}
        onOffsetChange={() => {}}
        busy
      />,
    );
    expect(screen.getByRole("button", { name: "Next" })).toHaveFocus();
  });

  it("full: numbered window navigates and marks the current page", async () => {
    const onOffsetChange = vi.fn();
    render(
      <Pagination
        total={48 * 48}
        offset={48 * 29}
        limit={48}
        onOffsetChange={onOffsetChange}
      />,
    );
    const current = screen.getByRole("button", { name: "Page 30" });
    expect(current).toHaveAttribute("aria-current", "page");
    await userEvent.click(screen.getByRole("button", { name: "Page 48" }));
    expect(onOffsetChange).toHaveBeenCalledWith(48 * 47);
  });

  it("full: the sm+ numbered window also announces loading for screen readers", () => {
    const { container, rerender } = render(
      <Pagination
        total={48 * 48}
        offset={48 * 29}
        limit={48}
        onOffsetChange={() => {}}
      />,
    );
    // The numbered window (sm:flex) is distinct from the below-sm readout
    // (sm:hidden) — scope into it specifically since jsdom renders both
    // regardless of the Tailwind breakpoint classes.
    const numberedWindow = container.querySelector(
      "span.hidden.items-center.gap-1",
    );
    expect(numberedWindow).not.toBeNull();
    expect(
      within(numberedWindow as HTMLElement).queryByText("Loading page…"),
    ).not.toBeInTheDocument();

    rerender(
      <Pagination
        total={48 * 48}
        offset={48 * 29}
        limit={48}
        onOffsetChange={() => {}}
        busy
      />,
    );
    expect(
      within(numberedWindow as HTMLElement).getByText("Loading page…"),
    ).toBeInTheDocument();
  });
});

describe("Pagination compact variant", () => {
  it("exports the page-size options", () => {
    expect(PAGE_SIZE_OPTIONS).toEqual([48, 96, 192]);
  });

  it("renders icon buttons and a page/pages readout", () => {
    render(
      <Pagination
        compact
        label="Pagination (top)"
        total={144}
        offset={48}
        limit={48}
        onOffsetChange={() => {}}
      />,
    );
    expect(
      screen.getByRole("navigation", { name: "Pagination (top)" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/2\s*\/\s*3/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Previous page" }),
    ).toBeEnabled();
    expect(screen.getByRole("button", { name: "Next page" })).toBeEnabled();
  });

  it("disables only the out-of-bounds icon button", () => {
    render(
      <Pagination
        compact
        total={96}
        offset={0}
        limit={48}
        onOffsetChange={() => {}}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Previous page" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next page" })).toBeEnabled();
  });

  it("advances and rewinds by limit, clamping at 0", () => {
    const onOffsetChange = vi.fn();
    render(
      <Pagination
        compact
        total={200}
        offset={30}
        limit={48}
        onOffsetChange={onOffsetChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(onOffsetChange).toHaveBeenLastCalledWith(78);
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    expect(onOffsetChange).toHaveBeenLastCalledWith(0);
  });

  it("swallows clicks while busy and swaps the readout for a spinner", () => {
    const onOffsetChange = vi.fn();
    const { container } = render(
      <Pagination
        compact
        total={96}
        offset={0}
        limit={48}
        onOffsetChange={onOffsetChange}
        busy
      />,
    );
    expect(
      screen.getByRole("navigation", { name: "Pagination" }),
    ).toHaveAttribute("aria-busy", "true");
    expect(container.querySelector(".animate-spin")).not.toBeNull();
    const next = screen.getByRole("button", { name: "Next page" });
    expect(next).toBeEnabled();
    fireEvent.click(next);
    expect(onOffsetChange).not.toHaveBeenCalled();
  });

  it("compact: first/last jump to the bounds", async () => {
    const onOffsetChange = vi.fn();
    render(
      <Pagination
        compact
        total={480}
        offset={96}
        limit={48}
        onOffsetChange={onOffsetChange}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "First page" }));
    expect(onOffsetChange).toHaveBeenCalledWith(0);
    await userEvent.click(screen.getByRole("button", { name: "Last page" }));
    expect(onOffsetChange).toHaveBeenCalledWith(432);
  });

  it("compact: the jump field is a plain typed field, not a number spinner", async () => {
    render(
      <Pagination
        compact
        total={48 * 48}
        offset={48}
        limit={48}
        onOffsetChange={() => {}}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    const input = screen.getByRole("textbox", { name: /go to page/i });
    // `type="number"` draws the UA spinner buttons — noise at toolbar size —
    // and, worse, steps its value on a mouse wheel: this field sits directly
    // above a scrolling grid, so a stray scroll would silently retarget the
    // page. A text field with a numeric keypad hint does neither.
    expect(input).toHaveAttribute("type", "text");
    expect(input).toHaveAttribute("inputmode", "numeric");
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    // The name still announces the range the field accepts.
    expect(input).toHaveAccessibleName("Go to page (1–48)");
    // It's the shadcn Input, so it arrives with that component's h-9 default
    // — the toolbar override has to actually win, or the field stands a
    // pixel taller than every button and select in the same band.
    expect(input.classList.contains("h-8")).toBe(true);
    expect(input.classList.contains("h-9")).toBe(false);
  });

  it("compact: the jump field takes digits only", async () => {
    // With the UA no longer policing the value (see above), the field itself
    // has to: typed letters are dropped rather than poisoning the commit.
    const onOffsetChange = vi.fn();
    render(
      <Pagination
        compact
        total={48 * 48}
        offset={48}
        limit={48}
        onOffsetChange={onOffsetChange}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    const input = screen.getByRole("textbox", { name: /go to page/i });
    await userEvent.clear(input);
    await userEvent.type(input, "3o0");
    expect(input).toHaveValue("30");
    await userEvent.type(input, "{Enter}");
    expect(onOffsetChange).toHaveBeenCalledWith(48 * 29);
  });

  it("compact: readout edits into a page jump", async () => {
    const onOffsetChange = vi.fn();
    render(
      <Pagination
        compact
        total={48 * 48}
        offset={48}
        limit={48}
        onOffsetChange={onOffsetChange}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    const input = screen.getByRole("textbox", { name: /go to page/i });
    await userEvent.clear(input);
    await userEvent.type(input, "30{Enter}");
    expect(onOffsetChange).toHaveBeenCalledWith(48 * 29);
    expect(onOffsetChange).toHaveBeenCalledTimes(1); // no double-commit via Enter+blur
    expect(screen.getByRole("button", { name: /go to page/i })).toHaveFocus();
  });

  it("compact: out-of-range commits clamp, Escape reverts", async () => {
    const onOffsetChange = vi.fn();
    render(
      <Pagination
        compact
        total={48 * 48}
        offset={48}
        limit={48}
        onOffsetChange={onOffsetChange}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    const input = screen.getByRole("textbox", { name: /go to page/i });
    await userEvent.clear(input);
    await userEvent.type(input, "999{Enter}");
    expect(onOffsetChange).toHaveBeenCalledWith(48 * 47); // clamped to last page
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /go to page/i })).toHaveFocus();
  });

  it("compact: Enter-commit that flips busy synchronously (like isFetching) still refocuses the readout", async () => {
    render(<BusyOnCommitHarness />);
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    const input = screen.getByRole("textbox", { name: /go to page/i });
    await userEvent.clear(input);
    await userEvent.type(input, "30{Enter}");
    // The readout stays mounted (as the busy spinner) rather than being
    // replaced by a separate element, so the deferred refocus has a live
    // node to land on.
    const readout = screen.getByRole("button", { name: /go to page/i });
    expect(readout).toBeInTheDocument();
    await waitFor(() => expect(document.activeElement).toBe(readout));
  });

  it("compact: clicking the readout while busy does not open the input", async () => {
    render(<BusyOnCommitHarness />);
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    const input = screen.getByRole("textbox", { name: /go to page/i });
    await userEvent.clear(input);
    await userEvent.type(input, "30{Enter}");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /go to page/i }),
      ).toHaveFocus(),
    );
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("compact: opening the jump field selects its value so typing replaces it", async () => {
    // Guard, not a regression: focus-select already held. The field is being
    // rebuilt, so pin that a single keystroke still overwrites the seeded
    // page number instead of appending to it.
    const onOffsetChange = vi.fn();
    render(
      <Pagination
        compact
        total={48 * 48}
        offset={48}
        limit={48}
        onOffsetChange={onOffsetChange}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    // The field seeds with "2" (offset 48 / limit 48 + 1), autofocuses and
    // selects it. Type straight at it — a userEvent.type() would click the
    // input first and collapse that selection, which a real user (already
    // focused by the open) never does.
    const input = screen.getByRole("textbox", { name: /go to page/i });
    expect(input).toHaveFocus();
    await userEvent.keyboard("7");
    expect(input).toHaveValue("7"); // replaced the "2", not appended to it
    await userEvent.keyboard("{Enter}");
    expect(onOffsetChange).toHaveBeenCalledWith(48 * 6); // page 7, not 27
  });
});

describe("PageSizeSelect", () => {
  it("renders every size option labelled 'N per page'", () => {
    render(<PageSizeSelect value={48} onChange={() => {}} />);
    const select = screen.getByRole("combobox", { name: "Results per page" });
    expect(select).toHaveValue("48");
    expect(
      screen.getByRole("option", { name: "48 per page" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "96 per page" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "192 per page" }),
    ).toBeInTheDocument();
  });

  it("emits the chosen size as a number", () => {
    const onChange = vi.fn();
    render(<PageSizeSelect value={48} onChange={onChange} />);
    fireEvent.change(
      screen.getByRole("combobox", { name: "Results per page" }),
      { target: { value: "96" } },
    );
    expect(onChange).toHaveBeenCalledWith(96);
  });
});
