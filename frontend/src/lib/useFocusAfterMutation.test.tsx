import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { useFocusAfterMutation } from "@/lib/useFocusAfterMutation";

/** Minimal removable list exercising the hook the way PlaylistDetailPage
 * will: each row registers its Remove button; removal requests focus on the
 * row that slides into the removed slot, then the previous row, then the
 * empty-state message. */
function RemovableList({
  initial,
  disabled = [],
}: {
  initial: string[];
  disabled?: string[];
}) {
  const [items, setItems] = useState(initial);
  const { register, requestFocus } = useFocusAfterMutation();

  function remove(index: number) {
    const next = items.filter((_, i) => i !== index);
    const candidates = [next[index], next[index - 1]]
      .filter((name): name is string => name !== undefined)
      .map((name) => `remove:${name}`);
    requestFocus(...candidates, "empty");
    setItems(next);
  }

  if (items.length === 0) {
    return (
      <p tabIndex={-1} ref={(el) => register("empty", el)}>
        Nothing left
      </p>
    );
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => setItems((current) => [...current])}
      >
        Refresh
      </button>
      <ul>
        {items.map((item, index) => (
          <li key={item}>
            {item}
            <button
              type="button"
              disabled={disabled.includes(item)}
              ref={(el) => register(`remove:${item}`, el)}
              onClick={() => remove(index)}
            >
              Remove {item}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

describe("useFocusAfterMutation", () => {
  it("focuses the row that slides into the removed slot", async () => {
    const user = userEvent.setup();
    render(<RemovableList initial={["a", "b", "c"]} />);
    await user.click(screen.getByRole("button", { name: "Remove b" }));
    expect(screen.getByRole("button", { name: "Remove c" })).toHaveFocus();
  });

  it("falls back to the previous row when removing the last item", async () => {
    const user = userEvent.setup();
    render(<RemovableList initial={["a", "b", "c"]} />);
    await user.click(screen.getByRole("button", { name: "Remove c" }));
    expect(screen.getByRole("button", { name: "Remove b" })).toHaveFocus();
  });

  it("skips disabled candidates", async () => {
    const user = userEvent.setup();
    render(<RemovableList initial={["a", "b", "c"]} disabled={["c"]} />);
    await user.click(screen.getByRole("button", { name: "Remove b" }));
    expect(screen.getByRole("button", { name: "Remove a" })).toHaveFocus();
  });

  it("falls back to the empty-state element when the list empties", async () => {
    const user = userEvent.setup();
    render(<RemovableList initial={["a"]} />);
    await user.click(screen.getByRole("button", { name: "Remove a" }));
    expect(screen.getByText("Nothing left")).toHaveFocus();
  });

  it("leaves focus alone when nothing was requested", async () => {
    const user = userEvent.setup();
    render(<RemovableList initial={["a", "b"]} />);
    const refresh = screen.getByRole("button", { name: "Refresh" });
    await user.click(refresh);
    expect(refresh).toHaveFocus();
  });
});
