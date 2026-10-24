"use client";

import { useQuery } from "@tanstack/react-query";
import { getKnowledgeBases } from "@/lib/api";

export function useKnowledgeBases() {
  return useQuery({ queryKey: ["knowledge-bases"], queryFn: getKnowledgeBases });
}
