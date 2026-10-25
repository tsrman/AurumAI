"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getAdminModels,
  getOrganizationImageModels,
  putOrganizationImageModels,
  putOrganizationModelSettings,
} from "@/lib/api";


export function useAdminModels() {
  return useQuery({
    queryKey: ["admin-models"],
    queryFn: getAdminModels,
  });
}


export function useUpdateOrganizationModelSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (settings: { defaultModel: string | null; defaultTemperature: number | null }) =>
      putOrganizationModelSettings(settings),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin-models"] }),
        queryClient.invalidateQueries({ queryKey: ["models"] }),
      ]);
    },
  });
}


export function useOrganizationImageModels() {
  return useQuery({
    queryKey: ["admin-image-models"],
    queryFn: getOrganizationImageModels,
  });
}


export function useUpdateOrganizationImageModels() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (settings: Parameters<typeof putOrganizationImageModels>[0]) =>
      putOrganizationImageModels(settings),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin-image-models"] }),
  });
}
