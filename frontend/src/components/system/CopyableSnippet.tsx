import { type ReactNode, useState } from "react";

import { Button } from "@/components/ui/button";

/** How long "Copied" stays on the button before it reverts. */
const COPIED_FOR_MS = 2000;

/**
 * Read-only text meant to be pasted somewhere else — a shell command, a config
 * block — with a labelled header row and a Copy button.
 *
 * One recipe, because there were two verbatim copies of it (slskd's webhook
 * config and the sign-in page's hash command) down to the same 2000ms timeout
 * and the same swallowed clipboard rejection. The keyboard fix below is the
 * reason that matters: a horizontally scrollable `<pre>` is unreachable without
 * a pointer unless it is focusable, and fixing that in two places means fixing
 * it in one and forgetting the other.
 */
export function CopyableSnippet({
  label,
  snippet,
  children,
}: Readonly<{
  /** Names the block, both visibly and as the scroll region's accessible
   * name — a bare "code" landmark tells a screen-reader user nothing. */
  label: string;
  /** The text shown and copied, verbatim. */
  snippet: string;
  /** Optional explanation between the header row and the snippet. */
  children?: ReactNode;
}>) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
      window.setTimeout(() => setCopied(false), COPIED_FOR_MS);
    } catch {
      // Clipboard access can be denied (insecure context / permission); the
      // text stays on screen to select by hand, so a failed copy is a no-op.
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium">{label}</p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void handleCopy()}
        >
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      {children}
      {/* `overflow-x-auto` makes this a scroll container, and a scroll
          container that only a pointer can reach fails WCAG 2.1.1. tabIndex
          puts it in the tab order; role+aria-label give the stop a name once
          it is there. */}
      <pre
        tabIndex={0}
        role="group"
        aria-label={label}
        className="bg-muted focus-ring overflow-x-auto rounded-lg p-3 font-mono text-xs"
      >
        {snippet}
      </pre>
    </div>
  );
}
