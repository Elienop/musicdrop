import { useSyncExternalStore } from "react";

// Counters bumped on each cross-tab `art:changed` event, used as a remount key
// on <img> elements (CoverArt, ArtistImage). A mounted <img> with an unchanged
// src won't re-request on its own, so query invalidation can't refresh the
// pixels in another tab. Remounting with the SAME url re-requests it, which
// (under the endpoints' no-cache + ETag) is a cheap conditional GET: 304 when
// unchanged, 200 when the art really changed. Bumped only on art:changed so
// ordinary library edits don't remount every image.
//
// Two levels, because an art event names WHICH asset changed:
//  - `globalVersion` — library-wide changes (a multi-artist art sweep, an SSE
//    re-connect catch-up). Bumping it refreshes every image.
//  - `scopedVersions` — one counter per asset key ("album:7", "artist:ABBA").
//    Installing one cover then remounts that one <img> instead of all 192 on a
//    full-page browse grid.
let globalVersion = 0;
const scopedVersions = new Map<string, number>();
const listeners = new Set<() => void>();

/** Bump the version consumers key their <img> on. Omit `scope` for a
 * library-wide refresh; pass `"album:7"` / `"artist:ABBA"` to move only the
 * consumers rendering that asset. */
export function bumpAssetVersion(scope?: string): void {
  if (scope === undefined) globalVersion += 1;
  else scopedVersions.set(scope, (scopedVersions.get(scope) ?? 0) + 1);
  // Every listener re-reads its own snapshot; only those whose value actually
  // moved re-render (useSyncExternalStore compares with Object.is).
  for (const l of listeners) l();
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  return () => listeners.delete(onChange);
}

/** Remount key for one asset. `scope` omitted → global-only (today's behavior,
 * for images whose identity the caller can't name). A GLOBAL bump moves every
 * consumer, scoped or not; a SCOPED bump moves only the matching ones. */
export function useAssetVersion(scope?: string): number {
  const read = (): number =>
    globalVersion + (scope === undefined ? 0 : (scopedVersions.get(scope) ?? 0));
  return useSyncExternalStore(subscribe, read, read);
}
