"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiResponse } from "@/types/api";

/** A skill Hermes Agent wrote for itself, waiting for or decided by an administrator. */
export interface HermesSkillDraft {
  id: string;
  path: string;
  name: string;
  description: string;
  status: "pending" | "approved" | "rejected";
  fileCount: number;
  sizeBytes: number;
  skillId: string | null;
  createdAt: string;
  updatedAt: string;
}

const KEY = ["hermes-skill-drafts"];

export function useHermesSkillDrafts(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: KEY,
    queryFn: async () =>
      (await api.get("hermes/skill-drafts").json<ApiResponse<HermesSkillDraft[]>>()).data,
    enabled: options?.enabled ?? true,
  });
}

export function useWithdrawHermesSkillDraft() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      await api.delete(`hermes/skill-drafts/${id}`);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEY }),
  });
}
