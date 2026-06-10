import { Outlet } from "react-router";

import { useActivity } from "@/api/useActivity";
import { ActivityButton } from "@/components/shell/ActivityPopover";
import { useActivityToasts } from "@/components/shell/activityToasts";
import { AppToaster } from "@/components/shell/AppToaster";
import { AppSidebar } from "@/components/shell/Sidebar";
import { AppTopbar, HealthStatus } from "@/components/shell/Topbar";
import { RouteAnnouncer } from "@/components/system/RouteAnnouncer";

/**
 * App shell (spec §2): persistent sidebar + topbar around the routed page.
 *
 * Layout: <AppSidebar> (nav sections; hidden below md — the topbar's
 * hamburger drawer covers small screens) beside a column of <AppTopbar>
 * (search · activity · health) over the scrolling <main>. Cross-page job
 * state lives in the topbar activity popover (rows over the existing polling
 * hooks) plus sonner toasts on running→done/failed transitions — the three
 * stacked job banners are gone. RouteAnnouncer makes navigation non-silent
 * (document.title + polite live region + h1 focus), and the skip link is the
 * first focusable element on every page.
 */
export function App() {
  const { rows } = useActivity();
  useActivityToasts(rows);
  return (
    <div className="bg-background text-foreground flex min-h-svh">
      <a
        href="#main-content"
        className="focus-ring sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:border focus:border-border focus:bg-background focus:px-3 focus:py-2 focus:text-sm"
      >
        Skip to content
      </a>
      <RouteAnnouncer />
      <AppSidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <AppTopbar>
          <ActivityButton />
          <HealthStatus />
        </AppTopbar>
        <main
          id="main-content"
          tabIndex={-1}
          className="mx-auto w-full max-w-8xl flex-1 px-6 py-6 outline-none"
        >
          <Outlet />
        </main>
      </div>
      <AppToaster />
    </div>
  );
}
