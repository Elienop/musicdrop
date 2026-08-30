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
 * and the same swallowed clipboard rejection. The snippet block below is the
 * reason that matters: getting its wrapping right in two places means getting
 * it right in one and forgetting the other.
 */
export function CopyableSnippet({
  label,
  snippet,
  children,
}: Readonly<{
  /** Names the block, in the header row directly above the snippet. */
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
      {/* This wraps rather than scrolling, which is why it needs no tabIndex,
          role or aria-label. `overflow-x-auto` made it a scroll container, and
          a scroll container only a pointer can reach fails WCAG 2.1.1 — so it
          then needed a tab stop, plus a role and a name to be worth landing
          on. Wrapping deletes the affordance instead of naming it: nothing is
          off-screen, so there is nothing to reach.

          It also fixes WCAG 1.4.10 (Reflow), which scrolling failed. Measured
          at a 312px content box (a 360px viewport): the slskd webhook block
          was 931px wide, hiding 619px — including the trailing
          `# host must be an IP…` comment that is the thing which makes the
          webhook work. The cost is 4 extra rows there at 360px, and 1 at
          desktop width; the Copy button remains the byte-exact path, so the
          rendered block only has to be readable, not re-typable.

          `break-words` is load-bearing: without it a token longer than the box
          (that URL) overflows and silently restores the scroll container. */}
      <pre className="bg-muted rounded-lg p-3 font-mono text-xs break-words whitespace-pre-wrap">
        {snippet}
      </pre>
    </div>
  );
}
