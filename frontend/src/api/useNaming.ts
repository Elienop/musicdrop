// frontend/src/api/useNaming.ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { components } from "./schema";
import { client } from "./client";

export type NamingConfig = components["schemas"]["NamingConfig"];
export type NamingRuleInput = components["schemas"]["NamingRuleInput"];
export type ReplaceRuleInput = components["schemas"]["ReplaceRuleInput"];
export type RenderedRule = components["schemas"]["RenderedRule"];
export type ReplaceError = components["schemas"]["ReplaceError"];
export type NamingPreviewResponse = components["schemas"]["NamingPreviewResponse"];
export type BeetsConfigSnapshot = components["schemas"]["BeetsConfigSnapshot"];

export const NAMING_KEY = ["config", "naming"] as const;

export interface NamingDraft {
  rules: NamingRuleInput[];
  replace: ReplaceRuleInput[];
}

export function useNaming() {
  return useQuery({
    queryKey: NAMING_KEY,
    queryFn: async (): Promise<NamingConfig> => {
      const { data, response } = await client.GET("/api/config/naming");
      if (!response.ok || !data) throw new Error("Failed to load naming config");
      return data;
    },
  });
}

export function usePreviewNaming() {
  return useMutation<NamingPreviewResponse, Error, NamingDraft>({
    mutationFn: async (body): Promise<NamingPreviewResponse> => {
      const { data, response } = await client.POST("/api/config/naming/preview", {
        body,
      });
      if (!response.ok || !data) throw new Error("Preview failed");
      return data;
    },
  });
}

/** Error carrying the HTTP status so the panel can special-case 409 (conflict). */
export interface NamingSaveError extends Error {
  status?: number;
}

export function useSaveNaming() {
  const qc = useQueryClient();
  return useMutation<
    BeetsConfigSnapshot,
    NamingSaveError,
    NamingDraft & { base_sha256: string }
  >({
    mutationFn: async (body): Promise<BeetsConfigSnapshot> => {
      const { data, response } = await client.POST("/api/config/naming/save", {
        body,
      });
      if (response.status === 409) {
        const e: NamingSaveError = new Error("Config changed on disk");
        e.status = 409;
        throw e;
      }
      if (!response.ok || !data) {
        const e: NamingSaveError = new Error("Failed to save naming config");
        e.status = response.status;
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
