"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiResponse } from "@/types/api";
import {
  Skill,
  SkillCreate,
  SkillUpdate,
  McpToolInfo,
  getSkills,
  createSkill,
  updateSkill,
  deleteSkill,
  discoverMcpTools,
  uploadCodeSkill,
  replaceCodeSkillBundle,
} from "@/lib/api";

export function useSkills() {
  return useQuery<ApiResponse<Skill[]>>({
    queryKey: ["skills"],
    queryFn: () => getSkills(),
  });
}

export function useCreateSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: SkillCreate) => createSkill(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

export function useUpdateSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: SkillUpdate }) =>
      updateSkill(id, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

export function useDeleteSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteSkill(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

export function useDiscoverMcpTools() {
  return useMutation<
    ApiResponse<McpToolInfo[]>,
    unknown,
    { endpoint: string; transport: string; authToken?: string; skillId?: string }
  >({
    mutationFn: ({ endpoint, transport, authToken, skillId }) =>
      discoverMcpTools(endpoint, transport, authToken, skillId),
  });
}

export function useUploadCodeSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (form: FormData) => uploadCodeSkill(form),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

export function useReplaceCodeSkillBundle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, form }: { id: string; form: FormData }) =>
      replaceCodeSkillBundle(id, form),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}
