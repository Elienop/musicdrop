// frontend/src/pages/settings/SettingsLayout.tsx
//
// The Settings shell (spec §1/§5): one PageHeader h1 for every /settings/*
// sub-route, a left sub-nav of section links (aria-current via NavLink,
// focus-ring, the sidebar's pill idiom), and an <Outlet/> for the section.
// Routes live in main.tsx; /settings itself index-redirects to /settings/beets.
import { NavLink, Outlet } from "react-router";

import { PageHeader } from "@/components/system/PageHeader";
import { cn } from "@/lib/utils";

const SETTINGS_SECTIONS = [
  { label: "Beets", to: "/settings/beets" },
  { label: "Naming", to: "/settings/naming" },
  { label: "Metadata", to: "/settings/metadata" },
  { label: "Integrations", to: "/settings/integrations" },
  { label: "Trash", to: "/settings/trash" },
] as const;

export function SettingsLayout() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Settings" />
      <div className="flex flex-col gap-6 md:flex-row md:gap-8">
        <nav aria-label="Settings sections" className="shrink-0 md:w-44">
          {/* Wraps horizontally below md, stacks as the left rail at md+. */}
          <ul className="flex flex-row flex-wrap gap-1 md:flex-col">
            {SETTINGS_SECTIONS.map((section) => (
              <li key={section.to}>
                <NavLink
                  to={section.to}
                  className={({ isActive }) =>
                    cn(
                      "focus-ring flex h-9 items-center rounded-md px-3 text-sm font-medium",
                      isActive
                        ? "bg-primary/15 text-primary-light"
                        : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
                    )
                  }
                >
                  {section.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
        <div className="min-w-0 flex-1">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
