import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
} from "@/components/ui/card";

/**
 * Pins the dense-by-default Card contract (UI redesign Phase 1): py-4/gap-3
 * shells and px-4 sections, so grid/panel consumers stop re-fighting the
 * stock shadcn py-6/gap-6/px-6 spacing on every use.
 */
describe("Card density defaults", () => {
  test("Card defaults to the dense py-4 / gap-3 shell", () => {
    render(<Card data-testid="card" />);
    const card = screen.getByTestId("card");
    expect(card).toHaveClass("py-4");
    expect(card).toHaveClass("gap-3");
    expect(card).not.toHaveClass("py-6");
    expect(card).not.toHaveClass("gap-6");
  });

  test("CardHeader, CardContent and CardFooter default to px-4", () => {
    render(
      <Card>
        <CardHeader data-testid="header" />
        <CardContent data-testid="content" />
        <CardFooter data-testid="footer" />
      </Card>,
    );
    for (const id of ["header", "content", "footer"]) {
      expect(screen.getByTestId(id)).toHaveClass("px-4");
      expect(screen.getByTestId(id)).not.toHaveClass("px-6");
    }
  });

  test("consumer overrides still win via tailwind-merge", () => {
    // The album-grid override pattern: cover-flush card with its own ramp.
    render(<Card data-testid="card" className="gap-2 py-0 pb-4" />);
    const card = screen.getByTestId("card");
    expect(card).toHaveClass("gap-2");
    expect(card).toHaveClass("py-0");
    expect(card).toHaveClass("pb-4");
    expect(card).not.toHaveClass("py-4");
    expect(card).not.toHaveClass("gap-3");
  });
});
