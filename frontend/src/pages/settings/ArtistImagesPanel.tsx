import { useArtistImageSettings, useSetArtistImageSettings } from "@/api/useArtistImage";
import { Spinner } from "@/components/icons";
import { SettingsSection } from "@/components/system/SettingsSection";
import { Switch } from "@/components/ui/switch";

/** Settings → Artist images: a persisted on/off toggle for the Deezer-backed
 * portrait fetch. Off = initials monograms everywhere + zero outbound calls. */
export function ArtistImagesPanel() {
  const settings = useArtistImageSettings();
  const setEnabled = useSetArtistImageSettings();
  const enabled = settings.data?.enabled ?? false;

  return (
    <SettingsSection
      title="Artist images"
      description="Fetch artist portraits from Deezer. When off, artists show their initials and no requests are made."
    >
      <div className="flex items-center gap-3">
        <Switch
          checked={enabled}
          disabled={settings.isPending || setEnabled.isPending}
          onCheckedChange={(v) => setEnabled.mutate(v)}
          aria-label="Enable artist images"
        />
        <span className="text-sm">{enabled ? "On" : "Off"}</span>
        {setEnabled.isPending && (
          <Spinner className="text-muted-foreground size-4 animate-spin" aria-hidden="true" />
        )}
        {setEnabled.isError && (
          <span className="text-destructive text-sm" role="alert">
            {setEnabled.error.message}
          </span>
        )}
      </div>
    </SettingsSection>
  );
}
