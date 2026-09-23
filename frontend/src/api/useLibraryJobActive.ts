import { useActiveImport } from "@/api/useActiveImport";
import { useArtistArtBackfillStatus } from "@/api/useArtistArt";
import { useDiskSyncStatus } from "@/api/useDiskSync";
import { useLyricsBackfillStatus } from "@/api/useLyricsBackfill";
import { useReorganizeStatus } from "@/api/useReorganize";

export type LibraryJobActive = {
  active: boolean;
  label: string | null;
  /** Ask every probe again. Most poll only while their job runs, so one
   * started elsewhere stays unseen until something refetches it — e.g. an
   * Apply that answered 409. */
  refetch: () => void;
};

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

  const refetch = () => {
    for (const probe of [importStatus, lyrics, artistArt, reorganize, diskSync]) {
      void probe.refetch();
    }
  };

  if (importStatus.data?.active) {
    return { active: true, label: "an import", refetch };
  }
  if (lyrics.data?.phase === "running") {
    return { active: true, label: "a lyrics backfill", refetch };
  }
  if (artistArt.data?.phase === "running") {
    return { active: true, label: "an artist-art backfill", refetch };
  }
  if (reorganize.data?.phase === "running") {
    return { active: true, label: "a library reorganize", refetch };
  }
  if (diskSync.data?.phase === "running") {
    return { active: true, label: "a disk sync", refetch };
  }
  return { active: false, label: null, refetch };
}
