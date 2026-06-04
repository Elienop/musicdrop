// frontend/src/pages/settings/ArtistImagesPanel.tsx
import { Loader2 } from "lucide-react";

import { useArtistImageSettings, useSetArtistImageSettings } from "@/api/useArtistImage";
import { Switch } from "@/components/ui/switch";

/** Settings → Artist images: a persisted on/off toggle for the Deezer-backed
 * portrait fetch. Off = initials monograms everywhere + zero outbound calls. */
export function ArtistImagesPanel() {
  const settings = useArtistImageSettings();
  const setEnabled = useSetArtistImageSettings();
  const enabled = settings.data?.enabled ?? false;

  return (
    <section
      aria-label="Artist images"
      className="border-border flex flex-col gap-3 rounded-xl border p-4"
    >
      <header className="flex flex-col gap-1">
        <h2 className="text-2xl font-semibold tracking-tight">Artist images</h2>
        <p className="text-muted-foreground text-sm">
          Fetch artist portraits from Deezer. When off, artists show their initials and no
          requests are made.
        </p>
      </header>
      <div className="flex items-center gap-3">
        <Switch
          checked={enabled}
          disabled={settings.isPending || setEnabled.isPending}
          onCheckedChange={(v) => setEnabled.mutate(v)}
          aria-label="Enable artist images"
        />
        <span className="text-sm">{enabled ? "On" : "Off"}</span>
        {setEnabled.isPending && (
          <Loader2 className="text-muted-foreground size-4 animate-spin" aria-hidden="true" />
        )}
        {setEnabled.isError && (
          <span className="text-destructive text-sm" role="alert">
            {setEnabled.error.message}
          </span>
        )}
      </div>
    </section>
  );
}
