import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiUrl, errorDetail } from "@/api/lib";
import type { components } from "@/api/schema";

type CoverInstallResult = components["schemas"]["CoverInstallResult"];

export type FetchedCover =
  | { found: false }
  | { found: true; blob: Blob; objectUrl: string; source: string | null };

export function useFetchAlbumCover(albumId: number) {
  return useMutation<FetchedCover, Error, void>({
    mutationFn: async () => {
      const res = await fetch(apiUrl(`/api/albums/${albumId}/cover/fetch`), { method: "POST" });
      if (res.status === 404) return { found: false };
      if (!res.ok) throw new Error("Fetch failed");
      const blob = await res.blob();
      return {
        found: true,
        blob,
        objectUrl: URL.createObjectURL(blob),
        source: res.headers.get("X-Art-Source"),
      };
    },
  });
}

export function useInstallAlbumCover(albumId: number) {
  const queryClient = useQueryClient();
  return useMutation<CoverInstallResult, Error, Blob>({
    mutationFn: async (image) => {
      const form = new FormData();
      form.append("file", image, "cover");
      const res = await fetch(apiUrl(`/api/albums/${albumId}/cover`), { method: "POST", body: form });
      if (res.status === 409) throw new Error("An import is running — try again when it finishes.");
      if (!res.ok) throw new Error(await errorDetail(res, "Cover install failed"));
      return (await res.json()) as CoverInstallResult;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["album", albumId] });
    },
  });
}
