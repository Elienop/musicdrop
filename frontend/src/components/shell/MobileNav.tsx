// frontend/src/components/shell/MobileNav.tsx
//
// Below `md` the sidebar is hidden; this hamburger + left sheet is the nav.
// Built on the ui/dialog primitives (the unified radix-ui package) with the
// centered-dialog positioning overridden into a left-anchored, full-height
// panel — cn()'s tailwind-merge lets the className below displace the base
// top/left/translate/rounded/padding utilities. Same NAV_SECTIONS data as
// AppSidebar, ≥44px (h-11) items, closes on navigation. Active-state pills
// are a sidebar affordance only — the sheet closes on tap, so it never
// represents "where you are".

import { useState } from "react";
import { Link } from "react-router";

import { Menu } from "@/components/icons";
import { NAV_ICONS, NAV_SECTIONS } from "@/components/shell/Sidebar";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

export function MobileNav() {
  const [open, setOpen] = useState(false);
  const close = () => setOpen(false);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="md:hidden"
          aria-label="Open navigation"
        >
          <Menu className="size-5" aria-hidden="true" />
        </Button>
      </DialogTrigger>
      <DialogContent
        // Radix warns without a description; the sr-only title is enough.
        aria-describedby={undefined}
        className={cn(
          // Re-anchor the centered dialog into a left, full-height sheet.
          "top-0 left-0 h-full w-72 max-w-none translate-x-0 translate-y-0 sm:max-w-none",
          "bg-surface-raised flex flex-col gap-1 overflow-y-auto rounded-none border-y-0 border-l-0 p-4",
          // Slide from the left edge (tw-animate enter/exit), gated for
          // reduced motion; the global prefers-reduced-motion block in
          // styles.css zeroes the durations as a second layer.
          "data-[state=open]:motion-safe:slide-in-from-left data-[state=closed]:motion-safe:slide-out-to-left",
        )}
      >
        <DialogTitle className="sr-only">Navigation</DialogTitle>
        {/* Brand: a link, not a heading — same contract as the sidebar. */}
        <Link
          to="/"
          onClick={close}
          className="focus-ring flex h-11 shrink-0 items-center rounded-md px-3 text-base font-semibold tracking-tight"
        >
          MusicDrop
        </Link>
        <nav aria-label="Primary">
          {NAV_SECTIONS.map((section) => (
            <div key={section.label}>
              <div className="text-muted-foreground/70 px-3 pt-4 pb-1.5 text-[11px] font-semibold tracking-wider uppercase">
                {section.label}
              </div>
              <ul className="flex flex-col gap-0.5">
                {section.items.map((item) => {
                  const Icon = NAV_ICONS[item.concept];
                  return (
                    <li key={item.to}>
                      <Link
                        to={item.to}
                        onClick={close}
                        className="focus-ring text-muted-foreground hover:bg-surface-hover hover:text-foreground flex h-11 items-center gap-3 rounded-md px-3 text-sm font-medium"
                      >
                        <Icon className="size-5 shrink-0" aria-hidden="true" />
                        <span>{item.label}</span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>
      </DialogContent>
    </Dialog>
  );
}
