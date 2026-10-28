"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiResponse } from "@/types/api";

/** Hermes Agent's notes (MEMORY.md) and its profile of the user (USER.md). */
export interface HermesMemory {
  memory: string;
  user: string;
}

const KEY = ["hermes-memory"];

export function useHermesMemory(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: KEY,
    queryFn: async () =>
      (await api.get("hermes/memory").json<ApiResponse<HermesMemory>>()).data,
    enabled: options?.enabled ?? true,
  });
}

export function useClearHermesMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await api.delete("hermes/memory");
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEY }),
  });
}
