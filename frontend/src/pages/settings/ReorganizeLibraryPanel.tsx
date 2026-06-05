import { ReorganizeControl } from "@/components/reorganize/ReorganizeControl";

export function ReorganizeLibraryPanel() {
  return (
    <section
      aria-label="Reorganize library"
      className="border-border flex flex-col gap-3 rounded-xl border p-4"
    >
      <header className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">Reorganize library</h2>
        <p className="text-muted-foreground text-sm">
          Re-apply your current path config to files already in the library — renames/moves folders so
          existing music matches your naming scheme. Edit the paths in the config editor above and{" "}
          <span className="font-medium">Apply</span> first; then preview before anything moves.
        </p>
      </header>
      <ReorganizeControl scope={{ scope: "library" }} />
    </section>
  );
}
