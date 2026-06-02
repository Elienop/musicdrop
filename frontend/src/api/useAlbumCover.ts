import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { components } from "@/api/schema";

type CoverInstallResult = components["schemas"]["CoverInstallResult"];

function apiUrl(path: string): string {
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  return `${origin}${path}`;
}

/** Read FastAPI's `{ "detail": ... }` error body, falling back to a generic
 * message when the response has no usable detail. */
async function errorDetail(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (
      body !== null &&
      typeof body === "object" &&
      "detail" in body &&
      typeof (body as { detail: unknown }).detail === "string"
    ) {
      return (body as { detail: string }).detail;
    }
  } catch {
    // Non-JSON body — fall through to the generic message.
  }
  return "Cover install failed";
}

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
      if (!res.ok) throw new Error(await errorDetail(res));
      return (await res.json()) as CoverInstallResult;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["album", albumId] });
    },
  });
}
