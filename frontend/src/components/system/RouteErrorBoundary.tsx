import { useEffect, useRef } from "react";
import { isRouteErrorResponse, Link, useRouteError } from "react-router";

import { Warning } from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
import { Button } from "@/components/ui/button";

/**
 * Styled route-level error boundary (data-router `errorElement`) — replaces
 * React Router's developer default ("Unexpected Application Error!" + raw
 * stack). Mounted on a pathless route INSIDE the App layout, so when a page
 * crashes the sidebar/topbar stay alive and the fallback renders where the
 * page would have. The error detail stays visible (muted, small) so bug
 * reports from the self-hosting user still carry the message.
 *
 * Not silent for AT: EmptyState deliberately carries no live-region role, so
 * the boundary owns the announcement — `role="alert"` on the message, plus an
 * h1 (tabIndex -1, focused on mount). Without these, RouteAnnouncer announces
 * the INTENDED page title on a crashing navigation (its `h1[tabindex="-1"]`
 * focus query would otherwise no-op), and an in-page crash unmounts the
 * focused element with no announcement at all.
 */
export function RouteErrorBoundary() {
  const error = useRouteError();
  const detail = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : "Unknown error";

  // Move focus to the boundary's own h1 on mount so keyboard/SR users land on
  // the error, not on <body> (in-page crash) or a stale target (navigation).
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <div role="alert" className="flex flex-col gap-4 py-10">
      {/* The visible title lives in EmptyState (a <p>); this sr-only h1
          doubles it as the page heading + focus target. */}
      <h1 ref={headingRef} tabIndex={-1} className="sr-only">
        Something went wrong
      </h1>
      <EmptyState
        bordered
        icon={Warning}
        title="Something went wrong"
        body="This page hit an unexpected error; the rest of the app is still fine. Reload the page, or head back to the overview."
        action={
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => window.location.reload()}
            >
              Reload page
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <Link to="/">Back to Overview</Link>
            </Button>
          </div>
        }
      />
      <p className="text-muted-foreground text-center font-mono text-xs break-all">
        {detail}
      </p>
    </div>
  );
}
