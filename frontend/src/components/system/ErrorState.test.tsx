// frontend/src/components/system/ErrorState.test.tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ErrorState } from "@/components/system/ErrorState";

test("hero (default): role=alert column with icon, message, and Retry wired", async () => {
  const user = userEvent.setup();
  const onRetry = vi.fn();
  render(<ErrorState message="Couldn't load albums." onRetry={onRetry} />);
  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent("Couldn't load albums.");
  expect(alert).toHaveClass("flex-col", "px-4", "py-16", "text-center");
  const svg = alert.querySelector("svg");
  expect(svg).not.toBeNull();
  expect(svg).toHaveClass("text-destructive", "size-10");
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(onRetry).toHaveBeenCalledTimes(1);
});

test("inline: single-row shape, still role=alert, Retry still present", async () => {
  const user = userEvent.setup();
  const onRetry = vi.fn();
  render(
    <ErrorState
      message="Couldn't load duplicates."
      onRetry={onRetry}
      variant="inline"
    />,
  );
  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent("Couldn't load duplicates.");
  expect(alert).toHaveClass("items-center", "justify-between", "p-4");
  expect(alert).not.toHaveClass("flex-col");
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(onRetry).toHaveBeenCalledTimes(1);
});

test("destructive tone chrome on both variants", () => {
  const { rerender } = render(
    <ErrorState message="x" onRetry={() => undefined} />,
  );
  expect(screen.getByRole("alert")).toHaveClass(
    "border-destructive/40",
    "bg-destructive/5",
  );
  rerender(
    <ErrorState message="x" onRetry={() => undefined} variant="inline" />,
  );
  expect(screen.getByRole("alert")).toHaveClass(
    "border-destructive/40",
    "bg-destructive/5",
  );
});
