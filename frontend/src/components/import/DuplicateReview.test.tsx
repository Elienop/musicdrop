import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { DuplicateActions } from "@/components/import/DuplicateReview";

describe("DuplicateActions", () => {
  test("the four actions are peer-weighted — none carries the primary accent", () => {
    render(<DuplicateActions pending={null} busy={false} onDecide={vi.fn()} />);
    for (const name of [/skip new/i, /keep both/i, /replace old/i, /merge/i]) {
      const button = screen.getByRole("button", { name });
      // shadcn's Button reflects its variant onto data-variant; none of the
      // four is preferred, so all use the neutral outline variant (Replace old
      // keeps only its amber caution tint on top of outline).
      expect(button).toHaveAttribute("data-variant", "outline");
    }
    // The accent (default) variant paints bg-primary — Merge must not.
    expect(screen.getByRole("button", { name: /merge/i })).not.toHaveClass(
      "bg-primary",
    );
  });
});
