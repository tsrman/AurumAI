"use client";

import { useQuery } from "@tanstack/react-query";
import { listWorkflows } from "@/lib/workflows-api";

export function useWorkflows(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ["workflows"],
    queryFn: listWorkflows,
    enabled: options?.enabled ?? true,
  });
}
