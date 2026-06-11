import type { ReactNode } from "react";

/**
 * Koito-style section label: small, uppercase, MUTED display-face text —
 * a label, not a headline (spec: hierarchy via size + color, not weight),
 * so it never competes with the content under it. Renders an h2 for the
 * document outline.
 */
export function SectionLabel({
  children,
  id,
}: {
  children: ReactNode;
  /** Optional anchor id (e.g. an aria-labelledby target). */
  id?: string;
}) {
  return (
    <h2
      id={id}
      className="font-display text-muted-foreground text-sm tracking-widest uppercase"
    >
      {children}
    </h2>
  );
}
