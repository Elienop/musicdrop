// frontend/src/api/useNaming.ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { components } from "./schema";
import { client } from "./client";
import { detailMessage, unwrap } from "./lib";
import { configOnDiskMessage } from "./useBeetsConfig";

export type NamingConfig = components["schemas"]["NamingConfig"];
export type NamingRuleInput = components["schemas"]["NamingRuleInput"];
export type ReplaceRuleInput = components["schemas"]["ReplaceRuleInput"];
export type RenderedRule = components["schemas"]["RenderedRule"];
export type ReplaceError = components["schemas"]["ReplaceError"];
export type NamingPreviewResponse =
  components["schemas"]["NamingPreviewResponse"];
export type BeetsConfigSnapshot = components["schemas"]["BeetsConfigSnapshot"];

export const NAMING_KEY = ["config", "naming"] as const;

export interface NamingDraft {
  rules: NamingRuleInput[];
  replace: ReplaceRuleInput[];
}

/** A load failure. `onDisk` is the server's sentence from the 422 about
 * config.yaml on disk; absent for any other failure. */
export interface NamingLoadError extends Error {
  onDisk?: string;
}

export function useNaming() {
  return useQuery<NamingConfig, NamingLoadError>({
    queryKey: NAMING_KEY,
    queryFn: async (): Promise<NamingConfig> => {
      const result = await client.GET("/api/config/naming");
      const onDisk =
        result.response.status === 422 ? detailMessage(result.error) : null;
      if (onDisk) {
        const e: NamingLoadError = new Error(onDisk);
        e.onDisk = onDisk;
        throw e;
      }
      return unwrap(result, "Failed to load naming config");
    },
  });
}

export function usePreviewNaming() {
  return useMutation<NamingPreviewResponse, Error, NamingDraft>({
    mutationFn: async (body): Promise<NamingPreviewResponse> =>
      unwrap(
        await client.POST("/api/config/naming/preview", { body }),
        "Preview failed",
      ),
  });
}

/** Error carrying the HTTP status so the panel can special-case 409 (conflict).
 * `onDisk` is the server's sentence from a `config_on_disk` 422 row; absent
 * for any other failure. */
export interface NamingSaveError extends Error {
  status?: number;
  onDisk?: string;
}

export function useSaveNaming() {
  const qc = useQueryClient();
  return useMutation<
    BeetsConfigSnapshot,
    NamingSaveError,
    NamingDraft & { base_sha256: string }
  >({
    mutationFn: async (body): Promise<BeetsConfigSnapshot> => {
      const { data, error, response } = await client.POST(
        "/api/config/naming/save",
        { body },
      );
      if (response.status === 409) {
        const e: NamingSaveError = new Error("Config changed on disk");
        e.status = 409;
        throw e;
      }
      if (!response.ok || !data) {
        // A 422 about config.yaml on disk carries the sentence to show.
        const onDisk = configOnDiskMessage(error);
        const e: NamingSaveError = new Error(
          onDisk ?? "Failed to save naming config",
        );
        e.status = response.status;
        if (onDisk) e.onDisk = onDisk;
        throw e;
      }
      return data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: NAMING_KEY });
      void qc.invalidateQueries({ queryKey: ["beets-config"] });
    },
  });
}
