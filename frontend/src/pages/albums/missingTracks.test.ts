import { describe, expect, it } from "vitest";
import type { Track } from "@/api/useAlbum";
import type { MissingReleaseTrack } from "@/api/useAlbumMissing";
import { buildDiscGroups } from "@/pages/albums/missingTracks";

function present(track: number, disc = 1, id = track): Track {
  return { id, title: `Track ${track}`, track, disc,
           duration_seconds: 100, artist: "A", mb_trackid: `t${track}`, has_lyrics: false,
           instrumental: false };
}
function missing(index: number, disc = 1): MissingReleaseTrack {
  return { index, disc, title: `Track ${index}`, duration_seconds: 100, mb_trackid: `t${index}` };
}

describe("buildDiscGroups", () => {
  it("interleaves missing rows by index within a disc", () => {
    const groups = buildDiscGroups([present(1), present(2), present(4)], [missing(3)]);
    expect(groups).toHaveLength(1);
    const kinds = groups[0].rows.map((r) => r.kind);
    expect(kinds).toEqual(["present", "present", "missing", "present"]);
    const indexes = groups[0].rows.map((r) =>
      r.kind === "present" ? r.track.track : r.track.index,
    );
    expect(indexes).toEqual([1, 2, 3, 4]);
  });

  it("groups across discs and slots missing into the right disc", () => {
    const groups = buildDiscGroups(
      [present(1, 1), present(1, 2, 11)],
      [missing(2, 2)],
    );
    expect(groups.map((g) => g.disc)).toEqual([1, 2]);
    expect(groups[1].rows.map((r) => r.kind)).toEqual(["present", "missing"]);
  });

  it("returns only present rows when there are no missing tracks", () => {
    const groups = buildDiscGroups([present(1), present(2)], []);
    expect(groups[0].rows.every((r) => r.kind === "present")).toBe(true);
  });
});
