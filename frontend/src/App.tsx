import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleSlash, Loader2 } from "lucide-react";
import { Link, Outlet } from "react-router";

import { client } from "@/api/client";
import { cn } from "@/lib/utils";

async function fetchHealth() {
  const { data, error } = await client.GET("/api/health");
  if (error || !data) {
    throw new Error("Health check failed");
  }
  return data;
}

/**
 * Compact backend health indicator in the header. Status is conveyed by THREE
 * carriers, not color alone (WCAG 1.4.1): a shape-distinct icon (check /
 * slashed-circle / spinner), a short visible text label, and the dot color.
 */
export function HealthStatus() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
  });

  const reachable = !isError && !isPending;
  const label = isPending ? "Checking" : reachable ? "Online" : "Offline";
  const description = isPending
    ? "Checking backend"
    : reachable
      ? `Backend online (v${data.version})`
      : "Backend unreachable";

  const Icon = isPending ? Loader2 : reachable ? CircleCheck : CircleSlash;

  return (
    <span
      className="flex items-center gap-1.5 text-sm"
      title={description}
      aria-label={description}
      role="status"
    >
      <Icon
        aria-hidden="true"
        className={cn(
          "size-4",
          isPending
            ? "text-muted-foreground animate-spin"
            : reachable
              ? "text-success"
              : "text-destructive",
        )}
      />
      <span
        className={cn(
          isPending
            ? "text-muted-foreground"
            : reachable
              ? "text-success"
              : "text-destructive",
        )}
      >
        {label}
      </span>
    </span>
  );
}

/**
 * App shell: persistent header chrome wrapping the routed page via `<Outlet>`.
 * Feature routes (Albums grid, album detail) render into the outlet.
 */
export function App() {
  return (
    <div className="bg-background text-foreground min-h-svh">
      <header className="border-border bg-background/80 sticky top-0 z-10 border-b backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-6 py-4">
          <h1 className="text-xl font-semibold tracking-tight">
            <Link
              to="/"
              className="focus-visible:ring-ring rounded-sm focus-visible:ring-2 focus-visible:outline-none"
            >
              MusicDrop
            </Link>
          </h1>
          <HealthStatus />
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
