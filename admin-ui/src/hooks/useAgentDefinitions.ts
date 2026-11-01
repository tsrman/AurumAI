"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type AgentDefinitionUpdate,
  archiveAgentDefinition,
  deleteAgentDefinition,
  getAgentDefinition,
  getAgentDefinitions,
  unarchiveAgentDefinition,
  updateAgentDefinition,
} from "@/lib/api";

// Shared with the workflows editor, which lists the same definitions.
const LIST_KEY = ["agent-definitions"];

export function useAgentDefinitions(includeArchived = false) {
  return useQuery({
    // The default list keeps the shared key, so pickers and the editor see one cache.
    queryKey: includeArchived ? [...LIST_KEY, "archived"] : LIST_KEY,
    queryFn: () => getAgentDefinitions(includeArchived),
  });
}

export function useAgentDefinition(id: string | null) {
  return useQuery({
    queryKey: ["agent-definition", id],
    queryFn: () => getAgentDefinition(id as string),
    enabled: id !== null,
  });
}

export function useUpdateAgentDefinition() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: AgentDefinitionUpdate }) =>
      updateAgentDefinition(id, data),
    onSuccess: (_result, { id }) => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
      void qc.invalidateQueries({ queryKey: ["agent-definition", id] });
    },
  });
}

export function useDeleteAgentDefinition() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteAgentDefinition(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: LIST_KEY }),
  });
}

export function useArchiveAgentDefinition() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, archived }: { id: string; archived: boolean }) =>
      archived ? archiveAgentDefinition(id) : unarchiveAgentDefinition(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: LIST_KEY }),
  });
}
