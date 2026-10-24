"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type AgentDefinitionUpdate,
  deleteAgentDefinition,
  getAgentDefinition,
  getAgentDefinitions,
  updateAgentDefinition,
} from "@/lib/api";

// Shared with the workflows editor, which lists the same definitions.
const LIST_KEY = ["agent-definitions"];

export function useAgentDefinitions() {
  return useQuery({ queryKey: LIST_KEY, queryFn: getAgentDefinitions });
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
