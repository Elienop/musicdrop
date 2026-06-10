import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PageBody, PageHeader } from "@/components/system/PageHeader";

describe("PageHeader", () => {
  it("renders the title as the page h1 with tabIndex -1", () => {
    render(<PageHeader title="Artists" />);
    const h1 = screen.getByRole("heading", { level: 1, name: "Artists" });
    expect(h1).toHaveAttribute("tabindex", "-1");
  });

  it("uses the page type scale on the h1", () => {
    render(<PageHeader title="Artists" />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveClass(
      "text-2xl",
      "font-bold",
    );
  });

  it("mounts the polite meta live region even when meta is omitted", () => {
    const { container } = render(<PageHeader title="Artists" />);
    const region = container.querySelector('[aria-live="polite"]');
    expect(region).not.toBeNull();
    expect(region).toBeEmptyDOMElement();
  });

  it("renders the meta line inside the live region", () => {
    const { container } = render(
      <PageHeader title="Artists" meta="12 artists" />,
    );
    expect(container.querySelector('[aria-live="polite"]')).toHaveTextContent(
      "12 artists",
    );
  });

  it("renders actions in the actions slot", () => {
    render(
      <PageHeader
        title="Playlists"
        actions={<button type="button">New playlist</button>}
      />,
    );
    expect(
      screen.getByRole("button", { name: "New playlist" }),
    ).toBeInTheDocument();
  });
});

describe("PageBody", () => {
  it("renders children at full width by default", () => {
    const { container } = render(
      <PageBody>
        <p>content</p>
      </PageBody>,
    );
    const body = container.firstElementChild;
    expect(body).not.toBeNull();
    expect(body).not.toHaveClass("max-w-3xl");
    expect(screen.getByText("content")).toBeInTheDocument();
  });

  it("caps and centers the narrow reading column", () => {
    const { container } = render(
      <PageBody variant="narrow">
        <p>content</p>
      </PageBody>,
    );
    const body = container.firstElementChild;
    expect(body).not.toBeNull();
    expect(body).toHaveClass("mx-auto", "max-w-3xl");
  });
});
