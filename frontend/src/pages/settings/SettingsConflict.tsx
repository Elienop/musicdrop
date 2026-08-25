import { yaml } from "@codemirror/lang-yaml";
import { MergeView } from "@codemirror/merge";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { useEffect, useRef } from "react";

import { Button } from "@/components/ui/button";

/**
 * Conflict resolution view: shown when Save returns 409 (the on-disk YAML
 * advanced past the CAS tokens we sent). The page hands us BOTH docs:
 *
 *  - `local`  — what the user has in their CM6 editor right now (the draft
 *               that lost the CAS race).
 *  - `server` — the freshest disk text the 409 body carried back.
 *
 * The user picks an exit:
 *
 *  - **Reload (drop my edits)** — abandon `local`, accept `server` as the
 *    new baseline. Page invalidates + re-fetches the snapshot.
 *  - **Overwrite anyway** — force-Save `local` with the server's fresh
 *    `sha256` token (carried by the 409 body) so the second Save can't lose
 *    the same race.
 *
 * The diff itself is a `@codemirror/merge` `MergeView`:
 *   - `a` side = `local`, editable in principle (but we don't surface the
 *     edits — Reload/Overwrite are the only two paths out). Keeping `a`
 *     editable preserves the revert affordance: `revertControls: "b-to-a"`
 *     means each changed chunk has a "<- revert" button that copies the
 *     server's version of that chunk INTO `a`, so a user who only wants to
 *     accept a subset of disk-side changes can still do that visually.
 *   - `b` side = `server`, locked read-only via the standard CM6 triplet
 *     (`EditorState.readOnly` + `EditorView.editable.of(false)`).
 *   - `collapseUnchanged: {}` folds identical regions to a "show more"
 *     affordance so the diff stays focused on the actual divergence.
 *   - `highlightChanges: true` + `gutter: true` colour the changed lines and
 *     give a per-chunk gutter marker.
 *
 * `MergeView` is a class instance that owns its own DOM under `parent`; we
 * mount it from a `useEffect` and MUST call `mv.destroy()` in the cleanup
 * or it leaks DOM nodes + listeners on unmount. The effect re-keys on the
 * actual doc strings so a fresh 409 (different `local` or `server`) spawns
 * a clean MergeView rather than trying to splice the new docs into the old
 * instance.
 */
export interface SettingsConflictProps {
  local: string;
  server: string;
  onReload: () => void;
  onOverwrite: () => void;
}

export function SettingsConflict({
  local,
  server,
  onReload,
  onOverwrite,
}: Readonly<SettingsConflictProps>) {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const mv = new MergeView({
      parent: host,
      a: {
        doc: local,
        extensions: [yaml()],
      },
      b: {
        doc: server,
        extensions: [
          yaml(),
          EditorState.readOnly.of(true),
          EditorView.editable.of(false),
        ],
      },
      // "b-to-a" = revert chunks FROM server (b) BACK INTO local (a) — i.e.
      // the user is editing the left side and can pull individual disk-side
      // chunks across. Direction matches the spec's "your edits on the
      // left, the on-disk version on the right" framing.
      revertControls: "b-to-a",
      highlightChanges: true,
      gutter: true,
      // Empty config = use the default (collapse runs of identical lines
      // with an "expand" affordance). Keeps long configs scannable.
      collapseUnchanged: {},
    });
    return () => {
      mv.destroy();
    };
  }, [local, server]);

  return (
    // Native <dialog> rendered inline via `open` — the same non-modal reality
    // the old role="dialog" div had (aria-modal was dropped: there was never a
    // focus trap, so claiming modality misled AT). `m-0 w-full text-foreground`
    // neutralize the UA dialog styles (auto margins, fit-content width,
    // CanvasText color) so the card's layout is unchanged.
    <dialog
      open
      aria-label="File changed on disk"
      className="border-destructive/40 bg-destructive/5 static m-0 flex w-full flex-col gap-3 rounded-xl border p-4 text-foreground"
    >
      <div className="flex flex-col gap-1">
        <p className="text-sm">
          <strong>File changed on disk</strong> while you were editing. Your
          edits are on the left; the on-disk version is on the right.
        </p>
        <p className="text-muted-foreground text-sm">
          Reload to drop your edits, or Overwrite to save yours anyway.
        </p>
      </div>
      {/* MergeView owns the DOM under this div — never render children
          through React, or the next mount will fight CM6 for the node. */}
      <div
        ref={hostRef}
        className="border-border max-h-[480px] overflow-auto rounded-md border"
      />
      <div className="flex flex-wrap gap-2">
        {/* Reload = primary recommended (safe) path: drop the in-flight draft
            and accept the on-disk version. Filled `default` variant carries
            the visual weight so a hurried user lands on the safe action.
            Overwrite = the dangerous path (force-clobber the on-disk file);
            `destructive` makes the warning colour signal the risk against
            the destructive-tinted panel chrome (`bg-destructive/5`). */}
        <Button variant="default" size="sm" onClick={onReload}>
          Reload (drop my edits)
        </Button>
        <Button variant="destructive" size="sm" onClick={onOverwrite}>
          Overwrite anyway
        </Button>
      </div>
    </dialog>
  );
}
