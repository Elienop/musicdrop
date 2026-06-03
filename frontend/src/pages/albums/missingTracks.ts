import type { Track } from "@/api/useAlbum";
import type { MissingReleaseTrack } from "@/api/useAlbumMissing";

/** A tracklist row: an owned library track, or an absent release track. */
export type TrackRowItem =
  | { kind: "present"; track: Track }
  | { kind: "missing"; track: MissingReleaseTrack };

export interface DiscGroup {
  disc: number;
  rows: TrackRowItem[];
}

/** Position key used to interleave present + missing rows within a disc. */
function position(row: TrackRowItem): number {
  return row.kind === "present" ? row.track.track : row.track.index;
}

/**
 * Merge owned tracks with absent release tracks into per-disc groups, ordered by
 * (disc, position). Present rows keep their API order; missing rows slot in by
 * their release index. Stable: when a present track and a missing track share a
 * position (shouldn't happen — a present track isn't missing), the present one
 * sorts first.
 */
export function buildDiscGroups(
  tracks: Track[],
  missing: MissingReleaseTrack[],
): DiscGroup[] {
  const rows: TrackRowItem[] = [
    ...tracks.map((track) => ({ kind: "present" as const, track })),
    ...missing.map((track) => ({ kind: "missing" as const, track })),
  ];
  const byDisc = new Map<number, TrackRowItem[]>();
  for (const row of rows) {
    const disc = row.track.disc; // both Track and MissingReleaseTrack carry `disc`
    const bucket = byDisc.get(disc);
    if (bucket) bucket.push(row);
    else byDisc.set(disc, [row]);
  }
  return [...byDisc.entries()]
    .sort(([a], [b]) => a - b)
    .map(([disc, groupRows]) => ({
      disc,
      rows: groupRows
        .map((row, i) => ({ row, i }))
        .sort((x, y) => {
          const d = position(x.row) - position(y.row);
          if (d !== 0) return d;
          // tie-break: present before missing, then original order (stable)
          if (x.row.kind !== y.row.kind) return x.row.kind === "present" ? -1 : 1;
          return x.i - y.i;
        })
        .map(({ row }) => row),
    }));
}
