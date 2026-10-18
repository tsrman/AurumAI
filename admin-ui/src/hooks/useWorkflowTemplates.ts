"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { workflowKeys } from "@/hooks/useWorkflows";
import {
  archiveWorkflowTemplate,
  captureWorkflowTemplate,
  getWorkflowTemplate,
  getWorkflowTemplates,
  instantiateWorkflowTemplate,
  replaceWorkflowTemplate,
} from "@/lib/workflow-templates-api";
import type {
  TemplateInstantiateRequest,
  TemplateReplaceRequest,
} from "@/lib/workflow-templates-api";
import { useAuth } from "@/providers/AuthProvider";

export const workflowTemplateKeys = {
  all: ["admin-workflow-templates"] as const,
  list: (orgId: string, includeArchived: boolean) =>
    ["admin-workflow-templates", "list", orgId, includeArchived] as const,
  detail: (orgId: string, id: string, version?: number) =>
    ["admin-workflow-templates", "detail", orgId, id, version ?? null] as const,
};

function useTemplateQueryAccess(enabled: boolean) {
  const { user, isLoading } = useAuth();
  const orgId = user?.orgId ?? "";
  return {
    orgId,
    enabled:
      enabled && !isLoading && orgId.length > 0 && user?.role === "org_admin",
  };
}

export function useWorkflowTemplates(includeArchived = false, enabled = true) {
  const access = useTemplateQueryAccess(enabled);
  return useQuery({
    queryKey: workflowTemplateKeys.list(access.orgId, includeArchived),
    queryFn: () => getWorkflowTemplates(includeArchived),
    enabled: access.enabled,
  });
}

export function useWorkflowTemplate(
  id: string | null,
  version: number | undefined,
  enabled = true
) {
  const access = useTemplateQueryAccess(enabled);
  return useQuery({
    queryKey: workflowTemplateKeys.detail(
      access.orgId,
      id ?? "",
      version
    ),
    queryFn: () => getWorkflowTemplate(id ?? ""),
    enabled: access.enabled && id !== null,
  });
}

function invalidateTemplates(queryClient: ReturnType<typeof useQueryClient>) {
  return queryClient.invalidateQueries({ queryKey: workflowTemplateKeys.all });
}

function responseStatus(error: unknown): number | undefined {
  if (typeof error !== "object" || error === null || !("response" in error)) {
    return undefined;
  }
  const response = error.response;
  return response instanceof Response ? response.status : undefined;
}

function invalidateConflict(
  queryClient: ReturnType<typeof useQueryClient>,
  error: unknown
) {
  if (responseStatus(error) === 409) return invalidateTemplates(queryClient);
}

export function useCaptureWorkflowTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: captureWorkflowTemplate,
    retry: false,
    onSuccess: () => invalidateTemplates(queryClient),
    onError: (error) => invalidateConflict(queryClient, error),
  });
}

export function useReplaceWorkflowTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: string;
      payload: TemplateReplaceRequest;
    }) => replaceWorkflowTemplate(id, payload),
    retry: false,
    onSuccess: () => invalidateTemplates(queryClient),
    onError: (error) => invalidateConflict(queryClient, error),
  });
}

export function useInstantiateWorkflowTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: string;
      payload: TemplateInstantiateRequest;
    }) => instantiateWorkflowTemplate(id, payload),
    retry: false,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: workflowTemplateKeys.all }),
        queryClient.invalidateQueries({ queryKey: workflowKeys.all }),
        queryClient.invalidateQueries({ queryKey: ["agent-definitions"] }),
      ]);
    },
    onError: (error) => invalidateConflict(queryClient, error),
  });
}

export function useArchiveWorkflowTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) =>
      archiveWorkflowTemplate(id, version),
    retry: false,
    onSuccess: () => invalidateTemplates(queryClient),
    onError: (error) => invalidateConflict(queryClient, error),
  });
}
