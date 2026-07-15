import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ReviewControlBar } from "@/components/import/ReviewControlBar";

function renderBar(over: Partial<React.ComponentProps<typeof ReviewControlBar>> = {}) {
  render(
    <ReviewControlBar
      decisions={[
        { key: "ignore", label: "Ignore", variant: "ghost", onClick: vi.fn(), disabled: false },
        {
          key: "asis",
          label: "Use as-is",
          variant: "secondary",
          onClick: vi.fn(),
          disabled: false,
          hinted: true,
        },
      ]}
      primary={{
        label: "Apply",
        pendingLabel: "Queuing…",
        pending: false,
        onClick: vi.fn(),
        disabled: false,
        icon: true,
      }}
      rescan={{ onClick: vi.fn(), pending: false, disabled: false }}
      search={{ onSearch: vi.fn(), busy: false, feedback: null, error: false }}
      hint="Use as-is keeps your tags."
      {...over}
    />,
  );
}

test("renders decisions, the tools cluster, and the primary", () => {
  renderBar();
  expect(screen.getByRole("button", { name: /^ignore$/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /use as-is/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /different release/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /rescan folder/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /apply/i })).toBeInTheDocument();
});

test("primary: null renders no primary button", () => {
  renderBar({ primary: null });
  expect(screen.queryByRole("button", { name: /apply/i })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /rescan folder/i })).toBeInTheDocument();
});

test("the search row is collapsed until the toggle opens it, with aria wiring", async () => {
  renderBar();
  const toggle = screen.getByRole("button", { name: /different release/i });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(
    screen.queryByRole("form", { name: /search for a different release/i }),
  ).not.toBeInTheDocument();
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  const form = screen.getByRole("form", { name: /search for a different release/i });
  expect(toggle).toHaveAttribute("aria-controls", form.id);
  await userEvent.click(toggle);
  expect(
    screen.queryByRole("form", { name: /search for a different release/i }),
  ).not.toBeInTheDocument();
});

test("defaultOpen starts with the search row visible", () => {
  renderBar({ search: { onSearch: vi.fn(), busy: false, feedback: null, error: false, defaultOpen: true } });
  expect(
    screen.getByRole("form", { name: /search for a different release/i }),
  ).toBeInTheDocument();
});

test("hinted decisions point aria-describedby at the hint line", () => {
  renderBar();
  const hint = screen.getByText("Use as-is keeps your tags.");
  expect(screen.getByRole("button", { name: /use as-is/i })).toHaveAttribute(
    "aria-describedby",
    hint.id,
  );
  expect(screen.getByRole("button", { name: /^ignore$/i })).not.toHaveAttribute(
    "aria-describedby",
  );
});

test("checking renders the library-check status line", () => {
  renderBar({ checking: true });
  expect(screen.getByRole("status")).toHaveTextContent(/checking your library/i);
});

test("a pending rescan shows Rescanning and disables itself", () => {
  renderBar({ rescan: { onClick: vi.fn(), pending: true, disabled: false } });
  expect(screen.getByRole("button", { name: /rescanning/i })).toBeDisabled();
});

test("a pending primary shows its pending label and messages render", () => {
  renderBar({
    primary: {
      label: "Apply",
      pendingLabel: "Queuing…",
      pending: true,
      onClick: vi.fn(),
      disabled: true,
      icon: true,
    },
    messages: <p role="alert">boom</p>,
  });
  expect(screen.getByRole("button", { name: /queuing/i })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("boom");
});
