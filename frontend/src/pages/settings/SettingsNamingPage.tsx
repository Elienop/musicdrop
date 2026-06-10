import { NamingPanel } from "@/pages/settings/NamingPanel";

/** Settings → Naming: the beets paths/replace editor with live preview. */
export function SettingsNamingPage() {
  return (
    <div className="flex flex-col gap-6">
      <NamingPanel />
    </div>
  );
}
