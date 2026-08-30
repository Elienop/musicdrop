import { type ReactNode, useState } from "react";

import { Button } from "@/components/ui/button";

/** How long "Copied" stays on the button before it reverts. */
const COPIED_FOR_MS = 2000;

/**
 * Shown INSTEAD of the Copy button where this browser will not give us the
 * clipboard at all.
 *
 * Names the CONDITION rather than the API, because the condition is the thing
 * the operator can act on: `navigator.clipboard` exists only in a secure
 * context, and MusicDrop's primary deployment — plain http on a LAN address
 * like http://192.168.1.10:3030 — is not one. `http://localhost` IS, which is
 * why the sentence lists both and why this cannot simply say "use HTTPS":
 * plenty of the people who would read it already have a working Copy button.
 */
const COPY_UNAVAILABLE =
  "Copying needs a secure page (HTTPS, or localhost), so select the text above to copy it by hand.";

/** Shown when the clipboard EXISTS and refused the write — a denied
 * permission, an unfocused document. A different cause from the above, so a
 * different first half; the same second half, because the way out is the
 * same and it is the half the reader needs. */
const COPY_REFUSED =
  "This browser refused the copy, so select the text above to copy it by hand.";

/**
 * Whether this page may use the clipboard at all.
 *
 * Read at render, not inside the click handler, so the button is never OFFERED
 * where it cannot work: on an insecure origin `navigator.clipboard` is
 * `undefined`, so `navigator.clipboard.writeText(...)` throws a TypeError that
 * the handler's catch swallowed — a button that silently did nothing, on the
 * first screen a new operator sees, for the one action its copy asks them to
 * take. The value cannot change for the life of a document (the origin
 * decides it), so there is nothing to subscribe to.
 */
function clipboardIsAvailable(): boolean {
  return typeof navigator.clipboard?.writeText === "function";
}

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
  const [refused, setRefused] = useState(false);
  const canCopy = clipboardIsAvailable();

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
      window.setTimeout(() => setCopied(false), COPIED_FOR_MS);
    } catch {
      // A PRESENT clipboard can still refuse the write — a denied permission,
      // a document that isn't focused. Same outcome for the user as an absent
      // one, so it gets the same sentence rather than the silence it used to:
      // the text is on screen and selectable, and now says so.
      setRefused(true);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium">{label}</p>
        {/* No button at all where the clipboard is unreachable, rather than a
            disabled one: `disabled` drops it out of the tab order, so the
            explanation below would be the only thing a keyboard or screen
            reader user ever reaches anyway — and a button that is present but
            dead is the shape this fix exists to delete. */}
        {canCopy && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void handleCopy()}
          >
            {copied ? "Copied" : "Copy"}
          </Button>
        )}
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
      {/* Below the block, because both sentences point AT it ("the text
          above") — and the fallback is only real because the block wraps and
          shows every character (see the note above). */}
      {!canCopy && (
        <p className="text-muted-foreground text-xs">{COPY_UNAVAILABLE}</p>
      )}
      {/* `<output>` IS role="status": this one appears in response to a click,
          so it has to announce itself rather than wait to be found. The
          unavailable line above is static from first paint and needs no live
          region. */}
      {refused && (
        <output className="text-muted-foreground text-xs">{COPY_REFUSED}</output>
      )}
    </div>
  );
}
