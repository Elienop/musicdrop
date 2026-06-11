import { SectionLabel } from "@/components/system/SectionLabel";
import type { ReactNode } from "react";

/**
 * Settings panel shell: a section-scale `h2` INSIDE a rounded-xl bordered
 * panel (the ReorganizeLibraryPanel shape — heading travels with its panel,
 * unlike the floating-heading-over-Card dialect). Headings here deliberately
 * demote from today's text-2xl panel titles to `text-base font-semibold`:
 * each route has exactly one h1 (PageHeader); everything below is a section.
 * `aria-label` names the landmark so heading-less navigation still works.
 */
export function SettingsSection({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={title}
      className="border-border flex flex-col gap-3 rounded-xl border p-4"
    >
      <header className="flex flex-col gap-1">
        <SectionLabel>{title}</SectionLabel>
        {description !== undefined && (
          <p className="text-muted-foreground text-sm">{description}</p>
        )}
      </header>
      {children}
    </section>
  );
}
