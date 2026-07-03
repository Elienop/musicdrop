import { useActiveImport } from "@/api/useActiveImport";
import { useArtistArtBackfillStatus } from "@/api/useArtistArt";
import { useDiskSyncStatus } from "@/api/useDiskSync";
import { useLyricsBackfillStatus } from "@/api/useLyricsBackfill";
import { useReorganizeStatus } from "@/api/useReorganize";

export type LibraryJobActive = { active: boolean; label: string | null };

/**
 * Whether a library job that BLOCKS a config Apply is currently running, plus a
 * human label for it. Mirrors the backend Apply gate (`config_editor.apply`):
 * an import, a lyrics backfill, an artist-art backfill, a reorganize, or a disk
 * sync — NOT acquisition (slskd downloads don't touch the beets reload, so they
 * don't block Apply). The config panels use this to disable Apply and say why, instead
 * of letting it fire and come back 409 with a misleading "restart" message.
 *
 * Reuses the existing status hooks (deduped by React Query), so no extra polling.
 */
export function useLibraryJobActive(): LibraryJobActive {
  const importStatus = useActiveImport();
  const lyrics = useLyricsBackfillStatus();
  const artistArt = useArtistArtBackfillStatus();
  const reorganize = useReorganizeStatus();
  const diskSync = useDiskSyncStatus();

  if (importStatus.data?.active) return { active: true, label: "an import" };
  if (lyrics.data?.phase === "running") {
    return { active: true, label: "a lyrics backfill" };
  }
  if (artistArt.data?.phase === "running") {
    return { active: true, label: "an artist-art backfill" };
  }
  if (reorganize.data?.phase === "running") {
    return { active: true, label: "a library reorganize" };
  }
  if (diskSync.data?.phase === "running") {
    return { active: true, label: "a disk sync" };
  }
  return { active: false, label: null };
}
