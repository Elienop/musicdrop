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
  coverAssetKey,
  title,
  subtitle,
  meta,
  badge,
  action,
  href,
  hrefState,
}: Readonly<{
  cover: string | null;
  /** Which library asset `cover` shows (e.g. `album:7`), when the caller knows
   * it — scopes cross-tab image remounts to that one album. See CoverArt. */
  coverAssetKey?: string;
  title: string;
  subtitle?: string;
  meta?: ReactNode;
  badge?: ReactNode;
  action?: ReactNode;
  href?: string;
  /** Router state for `href` (e.g. an `{ from: AlbumOrigin }` payload) so list
   * rows can thread navigation origin without a second link slot. */
  hrefState?: unknown;
}> ) {
  return (
    <div className="flex min-w-0 items-center gap-3 px-4 py-3">
      {/* Decorative — the adjacent title text names the album, so alt
          stays "" (CoverArt's default). */}
      <CoverArt
        src={cover}
        assetKey={coverAssetKey}
        className="size-10 shrink-0 rounded-md"
      />
      {/* Container query, not a breakpoint: this column's width depends on the
          cover, the action slot and whatever the caller wraps the row in, so
          the viewport cannot answer "does the subtitle line fit". Measured at
          a 768px viewport it is 323px wide in the import feed and 347px on
          /duplicates — narrower than at 640px, because the `md` sidebar opens
          in between. */}
      <div className="@container/rowtext flex min-w-0 flex-1 flex-col">
        <div className="flex min-w-0 items-center gap-2">
          <span className="min-w-0 truncate font-medium" title={title}>
            {href !== undefined ? (
              <Link
                to={href}
                state={hrefState}
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
          // Below 18rem of column, subtitle and meta take a line each. The
          // meta slot is max-content: 130.5px in the import feed, 185.1px on
          // /duplicates, so one line needs 218px / 272px for a 66px artist. At
          // 288px of column they leave the artist 136px / 82px. Those are
          // scrollbar-independent; the viewport→column mapping is not, so
          // re-derive it rather than quoting one. Swept 320→1920 in steps of 8
          // with a real 15px scrollbar: /duplicates runs 114px→1249px and
          // stacks at 488 and below; a /review bank row runs 0px→1163px, the 0
          // being a `needs_review` row at 320, which carries a fourth control.
          // gap-x only: a row gap would space the stacked lines apart, and is
          // inert on one line.
          // `overflow-hidden` confines this line's INK to the text column.
          // The meta span is `shrink-0`, so at the narrowest widths its ink ran
          // past the column and painted inside the action slot's buttons —
          // hit-tested with `elementFromPoint`, a tap on that text landed on
          // Ignore, a state-changing action. Clipping changes nothing about
          // which content wins the space; it stops invisible-to-the-layout ink
          // from taking a tap. It goes HERE and not on the column: the title
          // row above can hold a focusable link, whose focus ring this would
          // clip. `subtitle` is a string and no caller puts anything focusable
          // in `meta` (a ReactNode), which is what keeps that reasoning true of
          // this line too — the first one that does gets its ring clipped and
          // this box turned into a scroll container, so give it the title row's
          // treatment instead of relaxing the clip.
          <div className="text-muted-foreground @min-[18rem]/rowtext:flex-row @min-[18rem]/rowtext:items-center flex min-w-0 flex-col gap-x-2 overflow-hidden text-sm">
            {subtitle !== undefined && (
              <span className="min-w-0 truncate" title={subtitle}>
                {subtitle}
              </span>
            )}
            {subtitle !== undefined && meta !== undefined && (
              // Stacked, the line break already separates the two, and a
              // trailing middot would strand on the subtitle's line.
              <span aria-hidden="true" className="@min-[18rem]/rowtext:block hidden">
                ·
              </span>
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
