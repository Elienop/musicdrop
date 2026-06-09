import { LibraryDashboard } from "@/components/dashboard/LibraryDashboard";

/** The home (`/`): the library Overview — stats + recently added. The artists
 * roster lives at its own `/artists` route (reached via the Artists nav). */
export function HomePage() {
  return <LibraryDashboard />;
}
