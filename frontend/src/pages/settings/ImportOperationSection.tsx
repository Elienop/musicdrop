import type { ConfigOpError } from "@/api/useBeetsConfig";
import { applyRecoveryHint } from "@/api/useBeetsConfig";
import type { FileOperation } from "@/api/useImportOperation";
import { SettingsSection } from "@/components/system/SettingsSection";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { APPLY_FALLBACK } from "@/pages/settings/configFailureText";

const SWITCH_ID = "keep-downloads";
const HELP_ID = "keep-downloads-help";
const REASON_ID = "keep-downloads-reason";
const NOTE_ID = "keep-downloads-note";

/** Said under the switch when the loaded operation is one the switch does
 * not set. It shows before the first flip: on a `copy` config, on then off
 * lands on move, and the user should know that before pressing. */
const NOTE: Partial<Record<FileOperation, string>> = {
  copy: "Your config copies. This switch sets hardlink or move.",
  link: "Your config symlinks. This switch sets hardlink or move.",
  reflink: "Your config clones. This switch sets hardlink or move.",
  reflink_auto: "Your config clones. This switch sets hardlink or move.",
  in_place: "Your config imports in place. This switch sets hardlink or move.",
};

/** What the page knows that decides whether the switch can be used now. */
export interface ImportSwitchGate {
  /** A flip or an Apply is reloading beets; the page's own line says so. */
  reloading: boolean;
  /** The label of a running library job, or null. */
  job: string | null;
  /** The editor holds a draft, or its Save is in flight. */
  editing: boolean;
  /** A saved config.yaml waits for Apply. */
  applyPending: boolean;
  /** `GET /api/config` failed, so there is no file sha to send. */
  configUnreadable: boolean;
}

/**
 * The one reason line under the switch, first match, or null. Null while
 * beets reloads too: the switch is still off then, but the page's
 * `Reloading beets…` line already says why.
 */
export function importSwitchReason(gate: ImportSwitchGate): string | null {
  if (gate.reloading) return null;
  if (gate.job !== null) return `Available when ${gate.job} finishes.`;
  if (gate.editing) return "Save or cancel your edits first.";
  if (gate.applyPending) return "Apply saved changes first.";
  if (gate.configUnreadable) return "Fix config.yaml below.";
  return null;
}

/** True when the switch cannot be used now, with or without a reason line. */
export function importSwitchLocked(gate: ImportSwitchGate): boolean {
  return gate.reloading || importSwitchReason(gate) !== null;
}

/**
 * The sentence for a failed flip. A refusal the server words (a 409 or 422
 * with a string detail) is shown as sent. The Save's 409 conflict body says
 * the file changed; the refetch that follows every flip carries the new sha,
 * so trying again is the whole recovery. A 500 is Apply's, and says what
 * Apply's alert says.
 */
export function importSwitchFailure(err: ConfigOpError | Error): string {
  const status = "status" in err ? err.status : 0;
  const body = "body" in err ? err.body : undefined;
  const detail = (body as { detail?: unknown } | null | undefined)?.detail;
  if (status === 409 || status === 422) {
    if (typeof detail === "string" && detail.trim() !== "") return detail;
    if (
      status === 409 &&
      detail !== null &&
      typeof detail === "object" &&
      "current_sha256" in detail
    ) {
      return "config.yaml changed. Try again.";
    }
  }
  if (status >= 500) {
    return applyRecoveryHint(err as ConfigOpError) ?? APPLY_FALLBACK;
  }
  return "Couldn’t change the import setting. Try again.";
}

/**
 * Settings → Beets, first section: the Keep downloads switch. The page owns
 * the mutation and its gates, because a flip reloads beets exactly as Apply
 * does and the page's state machine has to show that. This renders the row.
 *
 * Gated, the switch stays focusable (`aria-disabled`, never `disabled`) and
 * the page swallows the change, so a keyboard user who pressed it keeps focus
 * on it and hears the reason line through `aria-describedby`. The primitive
 * styles only `disabled:`, hence `aria-disabled:opacity-50` here.
 */
export function ImportOperationSection({
  operation,
  loadFailed,
  ready,
  checked,
  locked,
  reason,
  failure,
  onCheckedChange,
}: Readonly<{
  /** What imports use now, once read. */
  operation: FileOperation | undefined;
  loadFailed: boolean;
  /** Both the operation and the config snapshot have answered. */
  ready: boolean;
  checked: boolean;
  locked: boolean;
  reason: string | null;
  failure: string | null;
  onCheckedChange: (on: boolean) => void;
}>) {
  if (loadFailed) {
    return (
      <SettingsSection title="Import">
        <p className="text-destructive text-sm" role="alert">
          Couldn&rsquo;t read the import setting.
        </p>
      </SettingsSection>
    );
  }

  const note = reason === null && operation ? NOTE[operation] : undefined;
  const describedBy = [
    HELP_ID,
    reason === null ? null : REASON_ID,
    note === undefined ? null : NOTE_ID,
  ]
    .filter((id) => id !== null)
    .join(" ");

  return (
    <SettingsSection title="Import">
      <div className="flex items-start gap-3">
        {ready ? (
          <Switch
            id={SWITCH_ID}
            checked={checked}
            onCheckedChange={onCheckedChange}
            aria-disabled={locked}
            aria-describedby={describedBy}
            className="aria-disabled:opacity-50"
          />
        ) : (
          // The switch's own box, never an unchecked switch: that would say
          // "move" before anything has been read.
          <Skeleton className="h-5 w-9 rounded-full" />
        )}
        <div className="flex flex-col gap-1">
          <label htmlFor={SWITCH_ID} className="text-sm font-medium">
            Keep downloads (hardlink)
          </label>
          <p id={HELP_ID} className="text-muted-foreground text-xs">
            Hardlinks share tag changes.
          </p>
          {reason !== null && (
            <p id={REASON_ID} className="text-muted-foreground text-xs">
              {reason}
            </p>
          )}
          {note !== undefined && (
            <p id={NOTE_ID} className="text-muted-foreground text-xs">
              {note}
            </p>
          )}
        </div>
      </div>
      {failure !== null && (
        <p className="text-destructive text-sm break-words" role="alert">
          {failure}
        </p>
      )}
    </SettingsSection>
  );
}
