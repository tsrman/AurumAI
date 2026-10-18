"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiResponse } from "@/types/api";
import {
  Group,
  GroupCreate,
  GroupUpdate,
  getGroups,
  createGroup,
  updateGroup,
  deleteGroup,
} from "@/lib/api";

export function useGroups() {
  return useQuery<ApiResponse<Group[]>>({
    queryKey: ["groups"],
    queryFn: getGroups,
  });
}

export function useCreateGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: GroupCreate) => createGroup(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["groups"] }),
  });
}

export function useUpdateGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: GroupUpdate }) =>
      updateGroup(id, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["groups"] }),
  });
}

export function useDeleteGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteGroup(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["groups"] }),
  });
}
