import type { ReleaseIdentity } from "@/api/useAlbum";
import { External } from "@/components/icons";

/**
 * Which release an album is — source · edition, label · disambiguation, and a
 * link out to the release page. Returns a fragment of plain-text lines (+ link)
 * meant to sit inside the album rail's stat stack. Renders nothing when the
 * identity is empty (an as-is or sparsely-tagged album).
 */
export function ReleaseInfo({ release }: Readonly<{ release: ReleaseIdentity }>) {
  const edition = [release.media, release.country].filter(Boolean).join(" · ");
  const source = [release.data_source, edition].filter(Boolean).join(" · ");
  const label = [release.label, release.disambiguation].filter(Boolean).join(" · ");
  if (!source && !label && !release.release_url) return null;
  return (
    <>
      {source && <span>{source}</span>}
      {label && <span>{label}</span>}
      {release.release_url && (
        <a
          href={release.release_url}
          target="_blank"
          rel="noreferrer"
          className="hover:text-foreground inline-flex w-fit items-center gap-1 underline underline-offset-4"
        >
          View release
          <External className="size-3" aria-hidden="true" />
          <span className="sr-only">(opens in a new tab)</span>
        </a>
      )}
    </>
  );
}
