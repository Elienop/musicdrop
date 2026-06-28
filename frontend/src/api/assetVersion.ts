import { useSyncExternalStore } from "react";

// A process-wide counter bumped on each cross-tab `art:changed` event, used as
// a remount key on <img> elements (CoverArt, ArtistImage). A mounted <img> with
// an unchanged src won't re-request on its own, so query invalidation can't
// refresh the pixels in another tab. Remounting with the SAME url re-requests
// it, which (under the endpoints' no-cache + ETag) is a cheap conditional GET:
// 304 when unchanged, 200 when the art really changed. Bumped only on
// art:changed so ordinary library edits don't remount every image.
let version = 0;
const listeners = new Set<() => void>();
export function bumpAssetVersion(): void {
  version += 1;
  for (const l of listeners) l();
}
function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  return () => listeners.delete(onChange);
}
export function useAssetVersion(): number {
  return useSyncExternalStore(subscribe, () => version, () => version);
}
