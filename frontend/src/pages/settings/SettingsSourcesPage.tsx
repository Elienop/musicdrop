import { FolderSourcesPanel } from "@/components/settings/FolderSourcesPanel";
import { SlskdPanel } from "@/components/settings/SlskdPanel";

/** Settings → Sources: where music comes from. slskd, whose finished
 * downloads import themselves, and the Folder sources Add from folder offers
 * as buttons. */
export function SettingsSourcesPage() {
  return (
    <div className="flex flex-col gap-6">
      <SlskdPanel />
      <FolderSourcesPanel />
    </div>
  );
}
