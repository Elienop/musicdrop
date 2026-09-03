import { AccountPanel } from "@/pages/settings/AccountPanel";

/** Settings → Account: this server's single password. Its own section rather
 * than a panel under Integrations or Beets — those are the external services
 * and the engine, and the account is neither. */
export function SettingsAccountPage() {
  return (
    <div className="flex flex-col gap-6">
      <AccountPanel />
    </div>
  );
}
