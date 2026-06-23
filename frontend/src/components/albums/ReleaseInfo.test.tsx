import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ReleaseIdentity } from "@/api/useAlbum";
import { ReleaseInfo } from "@/components/albums/ReleaseInfo";

const full: ReleaseIdentity = {
  data_source: "MusicBrainz",
  label: "Warner Bros.",
  country: "US",
  media: '12" Vinyl',
  disambiguation: "1973 reissue",
  release_url: "https://musicbrainz.org/release/abc",
};

describe("ReleaseInfo", () => {
  it("shows source · edition, label · disambiguation, and a release link", () => {
    render(<ReleaseInfo release={full} />);
    expect(screen.getByText('MusicBrainz · 12" Vinyl · US')).toBeInTheDocument();
    expect(screen.getByText("Warner Bros. · 1973 reissue")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /view release/i });
    expect(link).toHaveAttribute("href", "https://musicbrainz.org/release/abc");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("omits the link when there is no url", () => {
    render(<ReleaseInfo release={{ ...full, release_url: null }} />);
    expect(screen.queryByRole("link", { name: /view release/i })).not.toBeInTheDocument();
    expect(screen.getByText('MusicBrainz · 12" Vinyl · US')).toBeInTheDocument();
  });

  it("renders nothing when the identity carries no fields", () => {
    const empty: ReleaseIdentity = {
      data_source: null,
      label: null,
      country: null,
      media: null,
      disambiguation: null,
      release_url: null,
    };
    const { container } = render(<ReleaseInfo release={empty} />);
    expect(container).toBeEmptyDOMElement();
  });
});
