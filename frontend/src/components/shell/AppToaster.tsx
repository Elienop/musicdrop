// frontend/src/components/shell/AppToaster.tsx
import { Toaster } from "sonner";

/** The single sonner mount (spec §2): ONE live region, dark theme,
 * top-right. Rendered once in App by the shell-swap task — pages never
 * mount their own Toaster, and inline role=alert text remains the channel
 * for form-adjacent validation. */
export function AppToaster() {
  return <Toaster position="top-right" theme="dark" />;
}
