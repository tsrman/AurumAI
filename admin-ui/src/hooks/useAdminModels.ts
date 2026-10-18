"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getAdminModels,
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
