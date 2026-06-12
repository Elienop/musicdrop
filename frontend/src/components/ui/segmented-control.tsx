import { cn } from "@/lib/utils"

export interface SegmentedControlOption {
  value: string
  label: string
}

/**
 * A small exclusive-choice control: plain aria-pressed buttons in a
 * labeled group (no roving tabindex — Tab moves between options, which is
 * correct for a short row of real <button>s). Active option uses the
 * accent tint with `text-primary-light` — the spec's "active text on dark"
 * violet; base `text-primary` (#8b5cf6) only reaches ~4.1:1 on the tinted
 * surface, under WCAG AA. Never an inverted foreground.
 */
function SegmentedControl({
  options,
  value,
  onChange,
  "aria-label": ariaLabel,
}: {
  options: SegmentedControlOption[]
  value: string
  onChange: (v: string) => void
  "aria-label": string
}) {
  return (
    <div
      data-slot="segmented-control"
      role="group"
      aria-label={ariaLabel}
      className="bg-muted/50 inline-flex w-fit items-center gap-0.5 rounded-md border p-0.5 text-sm"
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          aria-pressed={value === option.value}
          className={cn(
            "focus-ring rounded-sm px-3 py-1 font-medium transition-colors",
            value === option.value
              ? "bg-primary/15 text-primary-light shadow-xs"
              : "text-muted-foreground hover:text-foreground"
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export { SegmentedControl }
