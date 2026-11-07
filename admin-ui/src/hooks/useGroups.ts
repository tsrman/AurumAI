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
  getGroupMembers,
  addGroupMembers,
  removeGroupMember,
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

export function useGroupMembers(
  groupId: string,
  params: { limit: number; offset: number; search?: string },
) {
  return useQuery({
    queryKey: ["group-members", groupId, params],
    queryFn: () => getGroupMembers(groupId, params),
  });
}

function useInvalidateMembers() {
  const qc = useQueryClient();
  // The group list shows the member count; the user directory filters by group.
  return () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ["group-members"] }),
      qc.invalidateQueries({ queryKey: ["groups"] }),
      qc.invalidateQueries({ queryKey: ["admin-users"] }),
    ]);
}

export function useAddGroupMembers() {
  const invalidate = useInvalidateMembers();
  return useMutation({
    mutationFn: ({ groupId, userIds }: { groupId: string; userIds: string[] }) =>
      addGroupMembers(groupId, userIds),
    onSuccess: invalidate,
  });
}

export function useRemoveGroupMember() {
  const invalidate = useInvalidateMembers();
  return useMutation({
    mutationFn: ({ groupId, userId }: { groupId: string; userId: string }) =>
      removeGroupMember(groupId, userId),
    onSuccess: invalidate,
  });
}
