import { ArtistArtPanel } from "@/pages/settings/ArtistArtPanel";
import { ArtistImagesPanel } from "@/pages/settings/ArtistImagesPanel";
import { LyricsBackfillPanel } from "@/pages/settings/LyricsBackfillPanel";

/** Settings → Metadata: lyrics backfill, artist images, artist art for Plex. */
export function SettingsMetadataPage() {
  return (
    <div className="flex flex-col gap-6">
      <LyricsBackfillPanel />
      <ArtistImagesPanel />
      <ArtistArtPanel />
    </div>
  );
}
