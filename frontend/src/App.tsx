import { useQuery } from "@tanstack/react-query";

import { client } from "@/api/client";
import { AlbumsPage } from "@/pages/albums/AlbumsPage";
import { cn } from "@/lib/utils";

async function fetchHealth() {
  const { data, error } = await client.GET("/api/health");
  if (error || !data) {
    throw new Error("Health check failed");
  }
  return data;
}

/** Small status dot in the header: green when the API is reachable, red when
 * not, muted while the first check is in flight. Replaces the 2a card. */
function HealthDot() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
  });

  const reachable = !isError && !isPending;
  const label = isPending
    ? "Checking backend"
    : isError
      ? "Backend unreachable"
      : `Backend ${data.status} (v${data.version})`;

  return (
    <span
      className="flex items-center gap-2"
      title={label}
      aria-label={label}
      role="status"
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-2 rounded-full",
          isPending
            ? "bg-muted-foreground/40"
            : reachable
              ? "bg-emerald-500"
              : "bg-destructive",
        )}
      />
    </span>
  );
}

export function App() {
  return (
    <div className="bg-background text-foreground min-h-svh">
      <header className="border-border bg-background/80 sticky top-0 z-10 border-b backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-6 py-4">
          <h1 className="text-xl font-semibold tracking-tight">MusicDrop</h1>
          <HealthDot />
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-8">
        <AlbumsPage />
      </main>
    </div>
  );
}
