import * as React from "react"
import { Checkbox as CheckboxPrimitive } from "radix-ui"

import { Confirm } from "@/components/icons"
import { cn } from "@/lib/utils"

function Checkbox({
  className,
  ...props
}: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        // The drawn box stays `size-4`; the TAP target is a 24px pseudo-element
        // centred on it (WCAG 2.2 SC 2.5.8, Level AA — decisions 40). It is out
        // of flow, so no row moves, and it carries no paint. Centred rather
        // than `-inset-*`: `inset` resolves against the PADDING box, which the
        // 1px border makes 14px, so an inset would have to encode 5px to mean
        // 24 — this says 24. It reaches 4px past the box on every side, which
        // every call site's neighbour clears.
        "peer relative size-4 shrink-0 rounded-[4px] border border-input shadow-xs transition-shadow outline-none before:absolute before:top-1/2 before:left-1/2 before:size-6 before:-translate-x-1/2 before:-translate-y-1/2 before:content-[''] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 data-[state=checked]:border-primary data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground",
        className
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator
        data-slot="checkbox-indicator"
        className="grid place-content-center text-current transition-none"
      >
        {/* weight="bold" — a regular-weight 14px check reads too thin inside
            the 16px control. */}
        <Confirm className="size-3.5" weight="bold" aria-hidden="true" />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

export { Checkbox }
