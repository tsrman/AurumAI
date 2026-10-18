"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  archiveWorkflow,
  createWorkflow,
  getAdminWorkflow,
  getAdminWorkflows,
  getWorkflowDiagnostic,
  replaceWorkflow,
  validateWorkflow,
} from "@/lib/api";
import type { WorkflowDraft } from "@/lib/api";

export const workflowKeys = {
  all: ["admin-workflows"] as const,
  list: (includeArchived: boolean) =>
    ["admin-workflows", "list", includeArchived] as const,
  detail: (id: string, version?: number) =>
    ["admin-workflows", "detail", id, version ?? null] as const,
  diagnostic: (runId: string) =>
    ["admin-workflows", "diagnostic", runId] as const,
};

export function useWorkflows(includeArchived = false) {
  return useQuery({
    queryKey: workflowKeys.list(includeArchived),
    queryFn: () => getAdminWorkflows(includeArchived),
  });
}

export function useWorkflow(id: string | null, version?: number) {
  return useQuery({
    queryKey: workflowKeys.detail(id ?? "", version),
    queryFn: () => getAdminWorkflow(id ?? ""),
    enabled: id !== null,
  });
}

function invalidateWorkflows(queryClient: ReturnType<typeof useQueryClient>) {
  return queryClient.invalidateQueries({ queryKey: workflowKeys.all });
}

export function useCreateWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createWorkflow,
    onSuccess: () => invalidateWorkflows(queryClient),
  });
}

export function useReplaceWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      version,
      draft,
    }: {
      id: string;
      version: number;
      draft: WorkflowDraft;
    }) => replaceWorkflow(id, version, draft),
    onSuccess: () => invalidateWorkflows(queryClient),
  });
}

export function useArchiveWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: archiveWorkflow,
    onSuccess: () => invalidateWorkflows(queryClient),
  });
}

export function useValidateWorkflow() {
  return useMutation({ mutationFn: validateWorkflow });
}

export function useWorkflowDiagnostic(runId: string | null) {
  return useQuery({
    queryKey: workflowKeys.diagnostic(runId ?? ""),
    queryFn: () => getWorkflowDiagnostic(runId ?? ""),
    enabled: runId !== null,
  });
}
