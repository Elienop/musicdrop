import type { ReactNode } from "react";

/**
 * Loading wrapper pairing skeleton bones with their announcement.
 *
 * The `role="status"` element must sit OUTSIDE the aria-hidden subtree, or
 * screen readers never hear the loading announcement (ImportCandidatePage
 * idiom). The bones wrapper is `display: contents` so the skeleton children
 * participate directly in the parent layout (flex gaps, grid tracks) exactly
 * like the loaded content they stand in for.
 */
export function PageSkeleton({
  announce,
  children,
}: {
  announce: string;
  children: ReactNode;
}) {
  return (
    <>
      <p className="sr-only" role="status">
        {announce}
      </p>
      <div className="contents" aria-hidden="true">
        {children}
      </div>
    </>
  );
}
