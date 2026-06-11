import { PageSkeleton } from "@/components/system/PageSkeleton";
import { Skeleton } from "@/components/ui/skeleton";

/** Suspense fallback while a lazy route chunk loads — the shared skeleton
 * dialect, generic enough for any page (title line + content block). */
export function RouteLoading() {
  return (
    <PageSkeleton announce="Loading page…">
      <div className="flex flex-col gap-6">
        <Skeleton className="h-10 w-48" />
        <Skeleton className="h-64 w-full rounded-xl" />
      </div>
    </PageSkeleton>
  );
}
