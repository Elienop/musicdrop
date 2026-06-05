import { useEffect, useState } from "react";

/**
 * Visibility gate that hides a (usually terminal) status after it has been
 * shown for `ms`, so a finished-job tally fades on its own instead of
 * lingering. Returns whether to render the status now.
 *
 * Resets whenever `resetKey` changes — pass the job id, so a new run re-arms
 * the message and shows a fresh tally.
 *
 * @param active   whether the status is eligible to show (e.g. a job reached a
 *                 terminal phase for this scope). While false, the timer is idle.
 * @param resetKey changing this restarts the timer (typically the job id)
 * @param ms       how long to keep it visible (default 8000)
 */
export function useAutoDismiss(active: boolean, resetKey: string | null, ms = 8000): boolean {
  const [dismissed, setDismissed] = useState(false);

  // A new job (or a fresh run) re-arms the message.
  useEffect(() => {
    setDismissed(false);
  }, [resetKey]);

  // Start the fade timer once the message is showing.
  useEffect(() => {
    if (!active || dismissed) return;
    const timer = setTimeout(() => setDismissed(true), ms);
    return () => clearTimeout(timer);
  }, [active, dismissed, ms]);

  return active && !dismissed;
}
