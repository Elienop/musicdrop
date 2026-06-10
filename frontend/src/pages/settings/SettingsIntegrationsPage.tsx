import { PlexSettingsPanel } from "@/components/settings/PlexSettingsPanel";
import { SlskdPanel } from "@/components/settings/SlskdPanel";

/** Settings → Integrations: Plex and slskd connections. */
export function SettingsIntegrationsPage() {
  return (
    <div className="flex flex-col gap-6">
      <PlexSettingsPanel />
      <SlskdPanel />
    </div>
  );
}
