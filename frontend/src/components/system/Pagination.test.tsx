import { fireEvent, render, screen } from "@testing-library/react";
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
