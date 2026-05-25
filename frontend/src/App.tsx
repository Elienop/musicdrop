import { HealthIndicator } from "@/components/HealthIndicator";

export function App() {
  return (
    <main className="bg-background text-foreground min-h-svh">
      <div className="mx-auto flex max-w-2xl flex-col gap-6 p-8">
        <header className="flex flex-col gap-1">
          <h1 className="text-3xl font-semibold tracking-tight">MusicDrop</h1>
          <p className="text-muted-foreground text-sm">
            Self-hosted web UI for beets
          </p>
        </header>
        <HealthIndicator />
      </div>
    </main>
  );
}
