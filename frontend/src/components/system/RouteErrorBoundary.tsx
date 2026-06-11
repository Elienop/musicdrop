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
 */
export function RouteErrorBoundary() {
  const error = useRouteError();
  const detail = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : "Unknown error";

  return (
    <div className="flex flex-col gap-4 py-10">
      <EmptyState
        bordered
        icon={Warning}
        title="Something went wrong"
        body="This page hit an unexpected error — the rest of the app is still fine. Reload the page, or head back to the overview."
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
