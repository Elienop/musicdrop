import { LibraryDashboard } from "@/components/dashboard/LibraryDashboard";
import { ArtistsPage } from "@/pages/artists/ArtistsPage";

/** The home (`/`): the library dashboard above the artists roster. */
export function HomePage() {
  return (
    <div className="flex flex-col gap-10">
      <LibraryDashboard />
      <ArtistsPage />
    </div>
  );
}
