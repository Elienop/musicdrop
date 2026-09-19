import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ReviewControlBar } from "@/components/import/ReviewControlBar";

function renderBar(over: Partial<React.ComponentProps<typeof ReviewControlBar>> = {}) {
  return render(bar(over));
}

/** The same element, so a test can rerender it and compare node identity. */
function bar(over: Partial<React.ComponentProps<typeof ReviewControlBar>> = {}) {
  return (
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
    />
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

// A live region that APPEARS already holding its sentence is not reliably
// announced: it has to be on the page, empty, before there is anything to say.
test("the library-check region is mounted before there is anything to say", () => {
  const view = renderBar({ checking: false });
  const region = screen.getByRole("status");
  expect(region).toBeEmptyDOMElement();

  view.rerender(bar({ checking: true }));
  expect(screen.getByRole("status")).toBe(region);
  expect(region).toHaveTextContent(/checking your library/i);
});

// The other half: a screen with no library check at all must not gain a second
// status region — the import candidate screen owns one of its own.
test("no checking prop mounts no library-check region", () => {
  renderBar();
  expect(screen.queryByRole("status")).toBeNull();
});

test("a pending rescan shows Rescanning and disables itself", () => {
  renderBar({ rescan: { onClick: vi.fn(), pending: true, disabled: false } });
  expect(screen.getByRole("button", { name: /rescanning/i })).toBeDisabled();
});

test("no search prop renders no Different release toggle", () => {
  renderBar({ search: undefined });
  expect(
    screen.queryByRole("button", { name: /different release/i }),
  ).not.toBeInTheDocument();
});

test("cluster renders in the bar and receives the hint id", async () => {
  renderBar({
    primary: null,
    cluster: (hintId) => (
      <button aria-describedby={hintId}>Replace old</button>
    ),
  });
  const btn = screen.getByRole("button", { name: /replace old/i });
  expect(btn).toHaveAttribute("aria-describedby", screen.getByText("Use as-is keeps your tags.").id);
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

// The decisions share one mutation with the primary, so the bar has to let the
// caller say which control was pressed — otherwise the primary is the only one
// that can report progress and it spins for somebody else's click.
test("a pending decision wears the posture, and its neighbours do not", () => {
  renderBar({
    decisions: [
      { key: "ignore", label: "Ignore", variant: "ghost", onClick: vi.fn(), disabled: true },
      {
        key: "asis",
        label: "Use as-is",
        variant: "secondary",
        onClick: vi.fn(),
        disabled: true,
        pending: true,
        pendingLabel: "Using as-is…",
      },
    ],
  });
  expect(screen.getByRole("button", { name: /using as-is…/i })).toBeDisabled();
  expect(
    screen.queryByRole("button", { name: /^use as-is$/i }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^ignore$/i })).toBeInTheDocument();
});

// The form the toggle reveals is already locked by `busy`; the toggle itself
// used to stay live, so the bar offered a dead panel.
test("the Different release toggle locks with the search it opens", () => {
  renderBar({
    search: { onSearch: vi.fn(), busy: true, feedback: null, error: false },
  });
  expect(
    screen.getByRole("button", { name: /different release/i }),
  ).toBeDisabled();
});
