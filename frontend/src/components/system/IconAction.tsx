import type { ComponentProps } from "react";

import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * Large icon action with its name in a tooltip (the Koito idiom): a ghost
 * icon-xl button, muted at rest, label revealed on hover AND keyboard focus.
 * The label doubles as the accessible name (aria-label), so screen readers
 * and tests see stable text even while the child swaps to a spinner. Pass
 * the glyph as children at `size-10`.
 */
export function IconAction({
  label,
  className,
  children,
  ...props
}: { label: string } & ComponentProps<typeof Button>) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xl"
          aria-label={label}
          className={cn(
            "text-muted-foreground hover:text-foreground",
            className,
          )}
          {...props}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
