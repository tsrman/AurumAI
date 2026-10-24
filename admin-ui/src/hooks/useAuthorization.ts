"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getAuthorizationSubjects,
  getCapabilityCatalog,
  getGroupCapabilities,
  getOrganizationCapabilities,
  getResourceGrants,
  previewAuthorization,
  putGroupCapabilities,
  putOrganizationCapabilities,
  reconcileResourceGrants,
} from "@/lib/api";
import type {
  AccessPreviewRequest,
  GrantReconcileRequest,
  ResourceType,
} from "@/lib/api";

export const authorizationKeys = {
  subjects: ["authorization", "subjects"] as const,
  catalog: ["authorization", "capability-catalog"] as const,
  organizationCapabilities: ["authorization", "capabilities", "organization"] as const,
  groupCapabilities: (groupId: string) =>
    ["authorization", "capabilities", "group", groupId] as const,
  grants: (resourceType: ResourceType, resourceId: string) =>
    ["authorization", "grants", resourceType, resourceId] as const,
};

export function useAuthorizationSubjects() {
  return useQuery({ queryKey: authorizationKeys.subjects, queryFn: getAuthorizationSubjects });
}

export function useCapabilityCatalog() {
  return useQuery({ queryKey: authorizationKeys.catalog, queryFn: getCapabilityCatalog });
}

export function useOrganizationCapabilities() {
  return useQuery({
    queryKey: authorizationKeys.organizationCapabilities,
    queryFn: getOrganizationCapabilities,
  });
}

export function usePutOrganizationCapabilities() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: putOrganizationCapabilities,
    onSuccess: (response) => {
      queryClient.setQueryData(authorizationKeys.organizationCapabilities, response);
    },
  });
}

export function useGroupCapabilities(groupId: string | null) {
  return useQuery({
    queryKey: authorizationKeys.groupCapabilities(groupId ?? ""),
    queryFn: () => getGroupCapabilities(groupId ?? ""),
    enabled: groupId != null,
  });
}

export function usePutGroupCapabilities() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ groupId, capabilities }: { groupId: string; capabilities: string[] }) =>
      putGroupCapabilities(groupId, capabilities),
    onSuccess: (response, variables) => {
      queryClient.setQueryData(authorizationKeys.groupCapabilities(variables.groupId), response);
    },
  });
}

export function useResourceGrants(resourceType: ResourceType, resourceId: string) {
  return useQuery({
    queryKey: authorizationKeys.grants(resourceType, resourceId),
    queryFn: () => getResourceGrants(resourceType, resourceId),
    enabled: resourceId.length > 0,
  });
}

export function useReconcileGrants() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: reconcileResourceGrants,
    onSuccess: (response, variables: GrantReconcileRequest) => {
      queryClient.setQueryData(
        authorizationKeys.grants(variables.resourceType, variables.resourceId),
        response
      );
      if (variables.resourceType === "model") {
        return Promise.all([
          queryClient.invalidateQueries({ queryKey: ["admin-models"] }),
          queryClient.invalidateQueries({ queryKey: ["models"] }),
        ]).then(() => undefined);
      }
    },
  });
}

export function useAccessPreview() {
  return useMutation({
    mutationFn: (payload: AccessPreviewRequest) => previewAuthorization(payload),
  });
}
