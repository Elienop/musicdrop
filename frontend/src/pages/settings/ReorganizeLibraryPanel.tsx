import { SettingsSection } from "@/components/system/SettingsSection";
import { ReorganizeControl } from "@/components/reorganize/ReorganizeControl";

export function ReorganizeLibraryPanel() {
  return (
    <SettingsSection
      title="Reorganize library"
      description="Re-apply your current path config to files already in the library — renames/moves folders so existing music matches your naming scheme. Edit paths in the config editor above or in Settings → Naming, then Apply; preview before anything moves."
    >
      <ReorganizeControl scope={{ scope: "library" }} />
    </SettingsSection>
  );
}
