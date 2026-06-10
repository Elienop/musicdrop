import { Link } from "react-router";

import { NotFound } from "@/components/icons";
import { EmptyState } from "@/components/system/EmptyState";
import { Button } from "@/components/ui/button";

/** Catch-all for unknown routes. One escape, one home: back to the Overview
 * (`/`) per spec §1. RouteAnnouncer already titles this route "Not found";
 * the EmptyState is the whole page (no PageHeader — there is no page here). */
export function NotFoundPage() {
  return (
    <EmptyState
      icon={NotFound}
      title="Page not found"
      body="That page doesn’t exist. Head back to your library."
      action={
        <Button variant="outline" size="sm" asChild>
          <Link to="/">Back to Overview</Link>
        </Button>
      }
    />
  );
}
