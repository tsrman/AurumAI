"use client";

import { useMutation } from "@tanstack/react-query";

import {
  searchKnowledgeBase,
  type KnowledgeSearchResponse,
} from "@/lib/knowledge-api";


export function useKnowledgeSearch(knowledgeBaseId: string) {
  const mutation = useMutation({
    mutationFn: (query: string) =>
      searchKnowledgeBase({
        knowledgeBaseId,
        query: query.trim(),
        limit: 10,
      }),
  });

  const search = async (query: string): Promise<KnowledgeSearchResponse> => {
    const trimmed = query.trim();
    if (!trimmed) return { results: [], warnings: [] };
    return mutation.mutateAsync(trimmed);
  };

  return { ...mutation, search };
}
