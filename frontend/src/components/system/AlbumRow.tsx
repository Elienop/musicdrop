import type { ReactNode } from "react";
import { Link } from "react-router";

import { CoverArt } from "@/components/system/CoverArt";

/**
 * The canonical compact album/folder row — one body for the import live
 * feed, Review decisions, duplicate-group members and future
 * download/search-result rows.
 *
 * Anatomy: [cover thumb] [title + badge / subtitle · meta] [action]. The
 * row is a role-free flex <div> so callers can wrap it in <li>, a table
 * cell or a plain list; highlight/selection styling stays with the caller
 * (e.g. `bg-primary/5` on the wrapping element).
 */
export function AlbumRow({
  cover,
  title,
  subtitle,
  meta,
  badge,
  action,
  href,
}: {
  cover: string | null;
  title: string;
  subtitle?: string;
  meta?: ReactNode;
  badge?: ReactNode;
  action?: ReactNode;
  href?: string;
}) {
  return (
    <div className="flex min-w-0 items-center gap-3 px-4 py-3">
      {/* Decorative — the adjacent title text names the album, so alt
          stays "" (CoverArt's default). */}
      <CoverArt src={cover} className="size-10 shrink-0 rounded-md" />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex min-w-0 items-center gap-2">
          <span className="min-w-0 truncate font-medium" title={title}>
            {href !== undefined ? (
              <Link
                to={href}
                className="focus-ring rounded-sm hover:underline"
              >
                {title}
              </Link>
            ) : (
              title
            )}
          </span>
          {/* Badge sits OUTSIDE the truncating span (shrink-0) so a long
              title can't clip it. */}
          {badge !== undefined && (
            <span className="flex shrink-0 items-center">{badge}</span>
          )}
        </div>
        {(subtitle !== undefined || meta !== undefined) && (
          <div className="text-muted-foreground flex min-w-0 items-center gap-2 text-sm">
            {subtitle !== undefined && (
              <span className="min-w-0 truncate" title={subtitle}>
                {subtitle}
              </span>
            )}
            {subtitle !== undefined && meta !== undefined && (
              <span aria-hidden="true">·</span>
            )}
            {meta !== undefined && (
              <span className="flex shrink-0 items-center">{meta}</span>
            )}
          </div>
        )}
      </div>
      {action !== undefined && (
        <div className="flex shrink-0 items-center gap-2">{action}</div>
      )}
    </div>
  );
}
