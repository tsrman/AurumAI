"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  approveHermesSkillDraft,
  getHermesSkillDraft,
  listHermesSkillDrafts,
  rejectHermesSkillDraft,
} from "@/lib/api";

const KEY = ["admin-hermes-skill-drafts"];

export function usePendingHermesSkillDrafts() {
  return useQuery({ queryKey: [...KEY, "pending"], queryFn: () => listHermesSkillDrafts("pending") });
}

export function useHermesSkillDraft(id: string | null) {
  return useQuery({
    queryKey: [...KEY, "detail", id],
    queryFn: () => getHermesSkillDraft(id as string),
    enabled: id != null,
  });
}

function useDecision<T>(decide: (variables: T) => Promise<unknown>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: decide,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KEY });
      // An approval adds or updates a code skill.
      queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
    // A refused decision (the draft changed or was decided meanwhile) shows
    // the draft as it is now.
    onError: () => queryClient.invalidateQueries({ queryKey: KEY }),
  });
}

export function useApproveHermesSkillDraft() {
  return useDecision(({ id, bundleChecksum, name }: { id: string; bundleChecksum: string; name?: string }) =>
    approveHermesSkillDraft(id, name ? { bundleChecksum, name } : { bundleChecksum })
  );
}

export function useRejectHermesSkillDraft() {
  return useDecision(({ id, bundleChecksum }: { id: string; bundleChecksum: string }) =>
    rejectHermesSkillDraft(id, { bundleChecksum })
  );
}
