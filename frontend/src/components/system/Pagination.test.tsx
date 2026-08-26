import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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

  it("full: the two gap separators carry distinct React keys", () => {
    // This window (page 30 of 48) renders BOTH a leading and a trailing gap,
    // so the two gap <span>s must carry different keys. React reports duplicate
    // keys via console.error, and this suite does not fail on that — assert it
    // explicitly.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(
        <Pagination
          total={48 * 48}
          offset={48 * 29}
          limit={48}
          onOffsetChange={() => {}}
        />,
      );
      const duplicateKeyCall = spy.mock.calls.find((call) =>
        call.some((arg) =>
          /duplicate key|two children with the same key/i.test(String(arg)),
        ),
      );
      expect(duplicateKeyCall).toBeUndefined();
    } finally {
      spy.mockRestore();
    }
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
    expect(input).toHaveAttribute("enterkeyhint", "go"); // labels the phone's Enter
    // The name still announces the range the field accepts — in ASCII, since
    // an en dash speaks unreliably across screen readers.
    expect(input).toHaveAccessibleName("Go to page (1 to 48)");
    // It's the shadcn Input, so it arrives with that component's h-9 default
    // — the toolbar override has to actually win, or the field stands a
    // pixel taller than every button and select in the same band.
    expect(input.classList.contains("h-8")).toBe(true);
    expect(input.classList.contains("h-9")).toBe(false);
    // But NOT the text size: Input ships `text-base md:text-sm` on purpose —
    // 16px on small screens is what stops iOS Safari zooming in on focus.
    // Pinning `text-sm` here would strip `text-base` and re-break that.
    expect(input.classList.contains("text-base")).toBe(true);
    expect(input.classList.contains("md:text-sm")).toBe(true);
  });

  it("compact: the readout announces WHERE you are, not just the range", () => {
    // An aria-label on this button takes precedence over its contents, so a
    // label carrying the range left assistive tech hearing "go to page 1 to
    // 67" and never the page it is on — and the focus contract deliberately
    // returns focus here after a commit, so this is the thing that has to say
    // where you landed. The full variant names itself by contents; so does
    // this one now.
    const { rerender } = render(
      <Pagination
        compact
        total={48 * 67}
        offset={48}
        limit={48}
        onOffsetChange={() => {}}
      />,
    );
    const readout = () => screen.getByRole("button", { name: /go to page/i });
    expect(readout()).toHaveAccessibleName("Go to page. Page 2 of 67");
    // The visible glyphs stay terse and are hidden from AT, so nobody hears
    // "2 slash 67" after the spoken sentence.
    expect(screen.getByText(/2\s*\/\s*67/)).toHaveAttribute(
      "aria-hidden",
      "true",
    );

    rerender(
      <Pagination
        compact
        total={48 * 67}
        offset={48}
        limit={48}
        onOffsetChange={() => {}}
        busy
      />,
    );
    // Busy: the name must still contain "go to page" (the focus contract
    // finds this button by that name) AND report the loading state.
    expect(readout()).toHaveAccessibleName("Go to page. Loading page…");
  });

  it("compact: the jump field opens showing the page you are on", async () => {
    render(
      <Pagination
        compact
        total={48 * 48}
        offset={48 * 4}
        limit={48}
        onOffsetChange={() => {}}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    // Seeded from the current page, so Enter alone is a no-op rather than a
    // jump to somewhere arbitrary, and you can edit one digit of it.
    expect(screen.getByRole("textbox", { name: /go to page/i })).toHaveValue(
      "5",
    );
  });

  it("compact: opening the jump field does not resize the pager", async () => {
    render(
      <Pagination
        compact
        total={48 * 48}
        offset={48}
        limit={48}
        onOffsetChange={() => {}}
      />,
    );
    const readout = screen.getByRole("button", { name: /go to page/i });
    await userEvent.click(readout);
    const input = screen.getByRole("textbox", { name: /go to page/i });
    // The readout is wider than a bare field (it grows with "48 / 48"), so a
    // field that REPLACED it would shift Next/Last left under the pointer
    // mid-click. Instead the readout stays mounted as the sizer and the field
    // lies on top of it. jsdom has no layout, so this pins the mechanism.
    expect(readout).toBeInTheDocument();
    expect(readout.classList.contains("invisible")).toBe(true);
    expect(input.parentElement).toBe(readout.parentElement);
    expect(input.classList.contains("absolute")).toBe(true);
    expect(input.classList.contains("inset-0")).toBe(true);
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

  it("compact: commits what the field SHOWS, not what React last saw", async () => {
    // React's value tracker suppresses onChange when a value is set the
    // programmatic way (`el.value = x` then an input event) — which is how
    // password managers, autofill and extensions write into fields. A commit
    // that reads component state would then send you to the page you were
    // already on while the field plainly displays another number.
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
    const input = screen.getByRole<HTMLInputElement>("textbox", {
      name: /go to page/i,
    });
    input.value = "40";
    fireEvent.input(input);
    expect(input).toHaveValue("40"); // what the user is looking at
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onOffsetChange).toHaveBeenCalledWith(48 * 39);
  });

  it("compact: clamps at BOTH ends, and only an empty field cancels", async () => {
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
    const open = async () =>
      await userEvent.click(
        screen.getByRole("button", { name: /go to page/i }),
      );
    const field = () => screen.getByRole("textbox", { name: /go to page/i });

    // "0" is as out-of-range as "999" and must behave the same way: clamp.
    // Falling through to a silent close makes it indistinguishable from
    // Escape, so the user gets no feedback that the page number was refused.
    await open();
    await userEvent.clear(field());
    await userEvent.type(field(), "0{Enter}");
    expect(onOffsetChange).toHaveBeenLastCalledWith(0); // page 1

    await open();
    await userEvent.clear(field());
    await userEvent.type(field(), "999{Enter}");
    expect(onOffsetChange).toHaveBeenLastCalledWith(48 * 47); // last page

    // Empty is the ONE non-commit: you cleared it and changed your mind.
    await open();
    await userEvent.clear(field());
    await userEvent.type(field(), "{Enter}");
    expect(onOffsetChange).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("compact: blurring out closes the field without committing", async () => {
    // The third exit path (Enter and Escape have their own tests). It must
    // not commit, and it must hand focus back like the other two.
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
    await userEvent.type(input, "30");
    fireEvent.blur(input);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(onOffsetChange).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /go to page/i }),
      ).toHaveFocus(),
    );
  });

  it("compact: clicking straight onto another control leaves focus there", async () => {
    // The activeElement === body guard. The deferred refocus exists for the
    // case where the closing field left focus on <body>; when the blur
    // happened because the user moved to a DIFFERENT control, that control
    // already owns focus and yanking it back a tick later is a bug.
    render(
      <>
        <Pagination
          compact
          total={48 * 48}
          offset={48}
          limit={48}
          onOffsetChange={() => {}}
        />
        <button type="button">Elsewhere</button>
      </>,
    );
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    const elsewhere = screen.getByRole("button", { name: "Elsewhere" });
    await userEvent.click(elsewhere);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    // Let the deferred refocus fire — it must decline to move focus.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(elsewhere).toHaveFocus();
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
