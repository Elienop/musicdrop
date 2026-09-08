import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { describe, expect, it } from "vitest";

import { AlbumRow } from "@/components/system/AlbumRow";
import {
  containerQueryVariants,
  unwiredContainerQueries,
} from "@/test/containerQuery";
import { renderWithProviders } from "@/test/render";

describe("AlbumRow", () => {
  it("renders title, subtitle, meta, badge and action slots", () => {
    render(
      <AlbumRow
        cover={null}
        title="OK Computer"
        subtitle="Radiohead"
        meta={<span>92% · Strong</span>}
        badge={<span>Needs review</span>}
        action={<button type="button">Review</button>}
      />,
    );
    expect(screen.getByText("OK Computer")).toBeInTheDocument();
    expect(screen.getByText("Radiohead")).toBeInTheDocument();
    expect(screen.getByText("92% · Strong")).toBeInTheDocument();
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Review" }),
    ).toBeInTheDocument();
  });

  it("truncates the title and subtitle", () => {
    render(
      <AlbumRow
        cover={null}
        title="A very long album title"
        subtitle="An artist"
      />,
    );
    expect(screen.getByText("A very long album title")).toHaveClass(
      "truncate",
    );
    expect(screen.getByText("An artist")).toHaveClass("truncate");
  });

  it("renders the cover thumbnail from the given src", () => {
    const { container } = render(
      <AlbumRow cover="/api/albums/7/cover" title="OK Computer" />,
    );
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img?.getAttribute("src")).toBe("/api/albums/7/cover");
  });

  it("wraps the title in a focus-ring link when href is given", () => {
    renderWithProviders(
      <AlbumRow cover={null} title="OK Computer" href="/albums/7" />,
    );
    const link = screen.getByRole("link", { name: "OK Computer" });
    expect(link).toHaveAttribute("href", "/albums/7");
    expect(link).toHaveClass("focus-ring");
  });

  it("renders plain text (no link) when href is omitted", () => {
    render(<AlbumRow cover={null} title="OK Computer" />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("threads hrefState as router state through the title link", async () => {
    function Probe() {
      const state = useLocation().state as { from?: { label: string } } | null;
      return <p>state: {state?.from?.label ?? "none"}</p>;
    }
    render(
      <MemoryRouter initialEntries={["/list"]}>
        <Routes>
          <Route
            path="/list"
            element={
              <AlbumRow
                cover={null}
                title="OK Computer"
                href="/albums/7"
                hrefState={{ from: { label: "Import", to: "/import?job=j1" } }}
              />
            }
          />
          <Route path="/albums/:albumId" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    await userEvent.click(screen.getByRole("link", { name: "OK Computer" }));
    expect(screen.getByText("state: Import")).toBeInTheDocument();
  });

  // The subtitle line stacks below 18rem of column. jsdom computes no layout,
  // so the widths are browser-measured and recorded in the component; what a
  // test CAN hold is that the variants are wired to a declared container —
  // rename one side and CSS reports nothing, the row silently keeps one arm.
  // The PRESENCE list is half the pin: an empty unwired list also means "no
  // variants here", so on its own it survives deleting the whole layer.
  it("wires every container-query variant to a declared container", () => {
    const { container } = render(
      <AlbumRow
        cover={null}
        title="OK Computer"
        subtitle="Radiohead"
        meta="76% · Medium match"
      />,
    );
    expect(containerQueryVariants(container)).toEqual([
      "@min-[18rem]/rowtext:block",
      "@min-[18rem]/rowtext:flex-row",
      "@min-[18rem]/rowtext:items-center",
    ]);
    expect(unwiredContainerQueries(container)).toEqual([]);
  });
});
