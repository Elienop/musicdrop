import { IconContext } from "@phosphor-icons/react";
import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";

import { useAuthGateState } from "@/api/auth";
import { ICON_WEIGHT, Spinner } from "@/components/icons";

/**
 * Admission to the app shell.
 *
 * Wraps `<App/>` in the layout route rather than living INSIDE App, because
 * App's first act is to mount the shell's data hooks (`useActivity`,
 * `useEventStream`) — and hooks cannot be skipped. A guard below them would
 * fire the whole shell fan-out on every cold load for a signed-out visitor,
 * producing a burst of 401s before the redirect it is supposed to prevent.
 * Outside App, nothing under the shell mounts until admission is granted.
 *
 * This is UX, not security: every gated route is refused server-side whatever
 * the client renders.
 */
export function RequireAuth({ children }: Readonly<{ children: ReactNode }>) {
  const location = useLocation();
  const state = useAuthGateState();

  if (state === "unknown") {
    return <AuthGateLoading />;
  }
  if (state === "unauthenticated") {
    // `from` rides along so signing in returns to the page that was asked for
    // instead of dumping everyone on the Overview. `replace` keeps the
    // unreachable URL out of history, so Back doesn't bounce here again.
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <>{children}</>;
}

/** The first paint before the status probe answers. Deliberately not the
 * shell: rendering sidebar and topbar here would flash a dead chrome that may
 * be about to be replaced by the sign-in page. */
function AuthGateLoading() {
  return (
    <IconContext.Provider value={ICON_WEIGHT}>
      <div className="bg-background text-foreground flex min-h-svh items-center justify-center">
        <output className="text-muted-foreground flex items-center gap-3 text-sm">
          <Spinner className="size-6 animate-spin" aria-hidden="true" />
          <span className="sr-only">Checking your session…</span>
        </output>
      </div>
    </IconContext.Provider>
  );
}
