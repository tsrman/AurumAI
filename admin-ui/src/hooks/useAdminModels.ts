"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getAdminModels,
  getOrganizationHermesSettings,
  getOrganizationImageModels,
  putOrganizationHermesSettings,
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


export function useOrganizationHermesSettings() {
  return useQuery({
    queryKey: ["admin-hermes-settings"],
    queryFn: getOrganizationHermesSettings,
  });
}


export function useUpdateOrganizationHermesSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (settings: Parameters<typeof putOrganizationHermesSettings>[0]) =>
      putOrganizationHermesSettings(settings),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["admin-hermes-settings"] }),
  });
}
