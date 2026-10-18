"use client";

import { useQuery } from "@tanstack/react-query";
import { ApiResponse } from "@/types/api";
import { Model, getModels } from "@/lib/api";

export function useModels(options?: { enabled?: boolean }) {
  return useQuery<ApiResponse<Model[]>>({
    queryKey: ["models"],
    queryFn: () => getModels(),
    enabled: options?.enabled ?? true,
  });
}
