import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RouteLoading } from "@/components/system/RouteLoading";

describe("RouteLoading", () => {
  it("announces and shows skeleton bones", () => {
    render(<RouteLoading />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading page…");
    // Bones are decorative.
    expect(document.querySelector('[aria-hidden="true"]')).not.toBeNull();
  });
});
