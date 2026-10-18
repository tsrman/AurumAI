"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  listKnowledgeBases,
  type KnowledgeBaseDto,
} from "@/lib/knowledge-api";
import { useAuth } from "@/providers/AuthProvider";
import type { KnowledgeBase } from "@/types/aurum";
import { LocalizedError } from "@/lib/errors";
import { useLocale } from "next-intl";

export interface UseKnowledgeReturn {
  knowledgeBases: KnowledgeBase[];
  createBase: (name: string, description: string) => Promise<KnowledgeBase>;
  deleteBase: (id: string) => Promise<void>;
  isLoading: boolean;
  isCreating: boolean;
  isDeleting: boolean;
}

function toKnowledgeBase(row: KnowledgeBaseDto, locale: string): KnowledgeBase {
  return {
    id: row.id,
    name: row.name,
    badge: row.name.slice(0, 2).toUpperCase(),
    badgeColor: "amber",
    description: row.description ?? "",
    canWrite: row.canWrite,
    docCount: 0,
    size: "—",
    updated: new Date(row.updatedAt).toLocaleDateString(locale),
    status: "indexed",
    docs: [],
  };
}

const KNOWLEDGE_BASES_KEY = ["knowledge-bases"] as const;

export function useKnowledge(): UseKnowledgeReturn {
  const { isAuthenticated } = useAuth();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: KNOWLEDGE_BASES_KEY,
    queryFn: listKnowledgeBases,
    enabled: isAuthenticated,
  });
  const createMutation = useMutation({
    mutationFn: createKnowledgeBase,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KNOWLEDGE_BASES_KEY });
    },
  });
  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteKnowledgeBase(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: KNOWLEDGE_BASES_KEY });
    },
  });
  const locale = useLocale();
  const knowledgeBases = useMemo(
    () => (query.data ?? []).map((row) => toKnowledgeBase(row, locale)),
    [query.data, locale]
  );

  const createBase = useCallback(
    async (name: string, description: string) => {
      const trimmedName = name.trim();
      const trimmedDescription = description.trim();
      if (!trimmedName) {
        throw new LocalizedError("knowledgeBaseNameRequired");
      }
      const row = await createMutation.mutateAsync({
        name: trimmedName,
        description: trimmedDescription,
      });
      return toKnowledgeBase(row, locale);
    },
    [createMutation, locale]
  );

  const deleteBase = useCallback(
    async (id: string) => {
      await deleteMutation.mutateAsync(id);
    },
    [deleteMutation]
  );

  return useMemo(
    () => ({
      knowledgeBases,
      createBase,
      deleteBase,
      isLoading: query.isLoading,
      isCreating: createMutation.isPending,
      isDeleting: deleteMutation.isPending,
    }),
    [
      knowledgeBases,
      createBase,
      deleteBase,
      query.isLoading,
      createMutation.isPending,
      deleteMutation.isPending,
    ]
  );
}
