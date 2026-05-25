import { Compass } from "lucide-react";
import { Link } from "react-router";

import { Button } from "@/components/ui/button";

/** Catch-all for unknown routes. Mirrors AlbumDetailPage's not-found treatment
 * (centered icon + message + outline escape) and sends the user back to the
 * roster home. */
export function NotFoundPage() {
  return (
    <div className="flex flex-col items-center gap-4 py-16 text-center">
      <Compass className="text-muted-foreground size-10" aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <p className="font-medium">Page not found</p>
        <p className="text-muted-foreground text-sm">
          That page doesn&rsquo;t exist. Head back to your library.
        </p>
      </div>
      <Button variant="outline" size="sm" asChild>
        <Link to="/">Back to artists</Link>
      </Button>
    </div>
  );
}
