import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePageSize } from "@/lib/usePageSize";

function Probe() {
  const { pageSize, setPageSize } = usePageSize();
  const location = useLocation();
  return (
    <div>
      <span data-testid="size">{pageSize}</span>
      <span data-testid="search">{location.search}</span>
      <button onClick={() => setPageSize(96)}>to96</button>
    </div>
  );
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Probe />
    </MemoryRouter>,
  );
}

describe("usePageSize", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("defaults to 48 with a clean URL and no stored preference", () => {
    renderAt("/browse");
    expect(screen.getByTestId("size").textContent).toBe("48");
  });

  it("honors a valid ?limit= from the URL", () => {
    renderAt("/browse?limit=96");
    expect(screen.getByTestId("size").textContent).toBe("96");
  });

  it("ignores a foreign ?limit= value", () => {
    renderAt("/browse?limit=200");
    expect(screen.getByTestId("size").textContent).toBe("48");
  });

  it("falls back to the stored preference when the URL has no limit", () => {
    localStorage.setItem("musicdrop.pageSize", "192");
    renderAt("/browse");
    expect(screen.getByTestId("size").textContent).toBe("192");
  });

  it("ignores an invalid stored preference", () => {
    localStorage.setItem("musicdrop.pageSize", "37");
    renderAt("/browse");
    expect(screen.getByTestId("size").textContent).toBe("48");
  });

  it("lets the URL win over the stored preference", () => {
    localStorage.setItem("musicdrop.pageSize", "192");
    renderAt("/browse?limit=96");
    expect(screen.getByTestId("size").textContent).toBe("96");
  });

  it("setPageSize persists, sets limit, and re-anchors offset in one update", () => {
    renderAt("/browse?limit=48&offset=144");
    fireEvent.click(screen.getByText("to96"));
    // floor(144 / 96) * 96 = 96 — the items on screen stay on screen.
    expect(screen.getByTestId("search").textContent).toBe(
      "?limit=96&offset=96",
    );
    expect(screen.getByTestId("size").textContent).toBe("96");
    expect(localStorage.getItem("musicdrop.pageSize")).toBe("96");
  });

  it("drops the offset param when the re-anchor lands on page 1", () => {
    renderAt("/browse?offset=48");
    fireEvent.click(screen.getByText("to96"));
    expect(screen.getByTestId("search").textContent).toBe("?limit=96");
  });

  it("still updates the URL when localStorage is unavailable", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    renderAt("/browse");
    fireEvent.click(screen.getByText("to96"));
    expect(screen.getByTestId("size").textContent).toBe("96");
    expect(screen.getByTestId("search").textContent).toBe("?limit=96");
  });
});
