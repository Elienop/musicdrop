import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  PageSizeSelect,
  Pagination,
} from "@/components/system/Pagination";

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
    const input = screen.getByRole("spinbutton", { name: /go to page/i });
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
    const input = screen.getByRole("spinbutton", { name: /go to page/i });
    await userEvent.clear(input);
    await userEvent.type(input, "999{Enter}");
    expect(onOffsetChange).toHaveBeenCalledWith(48 * 47); // clamped to last page
    await userEvent.click(screen.getByRole("button", { name: /go to page/i }));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /go to page/i })).toHaveFocus();
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
