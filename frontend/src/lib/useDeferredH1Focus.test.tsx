import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, test } from "vitest";

import { useDeferredH1Focus } from "@/lib/useDeferredH1Focus";

/** Skeleton→content flip: `ready=false` renders a heading-less skeleton (the
 * PageSkeleton situation on a cold load); `ready=true` renders the page h1.
 * The external button stands in for "a control the user reached first" and
 * stays mounted across the flip. */
function Harness({
  ready,
  withH1 = true,
}: {
  ready: boolean;
  withH1?: boolean;
}) {
  useDeferredH1Focus(ready);
  if (!ready) {
    return <p>skeleton</p>;
  }
  return withH1 ? (
    <h1 tabIndex={-1}>Loaded title</h1>
  ) : (
    <p>loaded, headingless</p>
  );
}

function harnessAt(props: { ready: boolean; withH1?: boolean }) {
  const ui = (p: { ready: boolean; withH1?: boolean }) => (
    <MemoryRouter initialEntries={["/albums/1"]}>
      <button type="button">External control</button>
      <Harness {...p} />
    </MemoryRouter>
  );
  const view = render(ui(props));
  return {
    rerenderWith: (p: { ready: boolean; withH1?: boolean }) =>
      view.rerender(ui(p)),
  };
}

describe("useDeferredH1Focus", () => {
  test("focuses the h1 when data lands while <body> holds focus", () => {
    const view = harnessAt({ ready: false });
    expect(document.body).toHaveFocus();

    view.rerenderWith({ ready: true });

    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
  });

  test("never steals focus from a control the user already reached", () => {
    const view = harnessAt({ ready: false });
    screen.getByRole("button", { name: "External control" }).focus();

    view.rerenderWith({ ready: true });

    expect(
      screen.getByRole("button", { name: "External control" }),
    ).toHaveFocus();
    expect(screen.getByRole("heading", { level: 1 })).not.toHaveFocus();
  });

  test("one-shot per pathname: a retry cycle never re-grabs focus", () => {
    const view = harnessAt({ ready: false });
    view.rerenderWith({ ready: true }); // first shot — h1 focused
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();

    // The user moves on; focus later falls back to <body>. A ready→false→true
    // flicker (error retry, remount churn) must NOT re-grab the h1.
    (document.activeElement as HTMLElement).blur();
    expect(document.body).toHaveFocus();
    view.rerenderWith({ ready: false });
    view.rerenderWith({ ready: true });

    expect(document.body).toHaveFocus();
  });

  test("a ready render without an h1 is a quiet no-op", () => {
    const view = harnessAt({ ready: false, withH1: false });
    view.rerenderWith({ ready: true, withH1: false });
    expect(document.body).toHaveFocus();
  });
});
