import { type RefObject, useEffect, useRef } from "react";
import { Link } from "react-router";

import { useSources } from "@/api/useSources";
import { Close, Folder } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { type RecentFolder, recentDayLabel } from "@/lib/useRecentFolders";

/**
 * Add from folder's Sources: one pin per Folder source, which fills the path
 * box and starts nothing. slskd never gets one: its message is the one way in
 * (decision #77), and `GET /api/sources` lists Folder sources only.
 *
 * Nothing while the list loads, fails or is empty. A source whose folder is
 * missing keeps its pin, `aria-disabled`, with `Missing` in its name; a pin
 * never checks more than that, since the start re-checks the path.
 */
export function SourcePins({
  onPick,
}: Readonly<{ onPick: (path: string) => void }>) {
  const sources = useSources().data?.sources ?? [];
  if (sources.length === 0) return null;

  return (
    // A fieldset is `min-inline-size: min-content` by default, which would let
    // a long pin widen the page: `min-w-0` lets it shrink to the column.
    <fieldset className="min-w-0">
      <legend className="mb-2 text-sm font-medium">Sources</legend>
      <div className="flex flex-wrap items-center gap-2">
        {sources.map((source) => (
          <Button
            key={source.id}
            type="button"
            variant="outline"
            size="sm"
            // The primitive is `whitespace-nowrap` and `shrink-0`: `max-w-full`
            // caps a 40-character name at the column, and the label truncates.
            // A missing pin takes no hover fill, as the primitive's own
            // `disabled:pointer-events-none` does; keyboard focus still reaches it.
            className="max-w-full aria-disabled:pointer-events-none aria-disabled:opacity-50"
            aria-disabled={source.exists ? undefined : true}
            onClick={() => {
              if (source.exists) onPick(source.folder);
            }}
          >
            <Folder aria-hidden="true" />
            <span className="min-w-0 truncate">{source.name}</span>
            {!source.exists && (
              <>
                {" "}
                <Badge variant="outline">Missing</Badge>
              </>
            )}
          </Button>
        ))}
        <Link
          to="/settings/sources"
          // The operation line's Change, in size and treatment.
          className="text-foreground focus-ring rounded-sm text-xs underline"
        >
          {/* "Change sources" to a screen reader: the operation line above
              has a Change of its own. The space sits outside the span, where
              every name computation keeps it. */}
          Change <span className="sr-only">sources</span>
        </Link>
      </div>
    </fieldset>
  );
}

/**
 * Add from folder's Recent folders, newest first. A row's path fills the path
 * box and starts nothing; its remove button drops it from this browser.
 *
 * Focus after a remove: the next row's remove button, else the previous one,
 * else the path box (the section is gone with its last row).
 */
export function RecentFolderList({
  recent,
  onPick,
  onRemove,
  fieldRef,
}: Readonly<{
  recent: readonly RecentFolder[];
  onPick: (path: string) => void;
  onRemove: (path: string) => void;
  fieldRef: RefObject<HTMLInputElement | null>;
}>) {
  const removeButtons = useRef(new Map<string, HTMLButtonElement>());
  // The removed row's neighbours by path, next first: a remove re-reads
  // storage, so another tab's add can shift every index in between.
  const pendingFocus = useRef<(string | undefined)[] | null>(null);

  useEffect(() => {
    const neighbours = pendingFocus.current;
    if (neighbours === null) return;
    pendingFocus.current = null;
    const button = neighbours
      .map((path) =>
        path === undefined ? undefined : removeButtons.current.get(path),
      )
      .find((el) => el !== undefined);
    (button ?? fieldRef.current)?.focus();
  }, [recent, fieldRef]);

  if (recent.length === 0) return null;

  return (
    <fieldset className="min-w-0">
      <legend className="mb-1 text-sm font-medium">Recent</legend>
      <ul className="flex flex-col divide-y">
        {recent.map((row, index) => (
          <li key={row.path} className="flex items-start gap-3 py-2">
            <div className="flex min-w-0 flex-1 flex-col items-start gap-0.5">
              {/* A plain button, not the primitive: that one is
                  `whitespace-nowrap` and a path must wrap. `w-full`: the row's
                  whole width picks the path, not only its text. */}
              <button
                type="button"
                className="focus-ring w-full min-w-0 rounded-sm text-left font-mono text-sm wrap-anywhere hover:underline"
                onClick={() => onPick(row.path)}
              >
                {row.path}
              </button>
              <span className="text-muted-foreground text-xs">
                {recentDayLabel(row.at)}
              </span>
            </div>
            {/* `-my-1.5`: half the 12px by which the size-8 button exceeds the
                20px path line, so it sits level with the first line
                (FolderSourcesPanel's remove button). */}
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={`Remove ${row.path} from Recent`}
              className="-my-1.5 shrink-0"
              ref={(el) => {
                if (el === null) removeButtons.current.delete(row.path);
                else removeButtons.current.set(row.path, el);
              }}
              onClick={() => {
                pendingFocus.current = [
                  recent[index + 1]?.path,
                  recent[index - 1]?.path,
                ];
                onRemove(row.path);
              }}
            >
              <Close aria-hidden="true" />
            </Button>
          </li>
        ))}
      </ul>
    </fieldset>
  );
}
