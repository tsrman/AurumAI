"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  attachFileToConversation,
  deleteDocument,
  listDocuments,
  uploadDocument,
  type DocumentDto,
} from "@/lib/documents-api";

const documentsKey = (knowledgeBaseId: string) =>
  ["documents", knowledgeBaseId] as const;

export function useDocuments(
  knowledgeBaseId: string,
  options?: { enabled?: boolean }
) {
  return useQuery<DocumentDto[]>({
    queryKey: documentsKey(knowledgeBaseId),
    queryFn: () => listDocuments(knowledgeBaseId),
    enabled: Boolean(knowledgeBaseId) && (options?.enabled ?? true),
    // Background Docling/chunking/embedding transitions pending→indexing→indexed
    // after the upload response. While unfinished documents remain,
    // refresh their status explicitly.
    refetchInterval: (query) => {
      const docs = query.state.data;
      const pending = docs?.some((d) => d.status === "pending" || d.status === "indexing");
      return pending ? 3000 : false;
    },
  });
}

export function useUploadDocument(knowledgeBaseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => uploadDocument({ knowledgeBaseId, file }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: documentsKey(knowledgeBaseId) });
    },
  });
}

/** Attach a file to a chat conversation — the backend lazily provisions a
 * hidden knowledge base for it on the first call and reuses that same one
 * on every later call for this conversation. Takes conversationId per-call
 * (not hook-level) since the composer may only learn the id — via
 * ensureConversationId — in the same gesture that starts the attach. The
 * composer reuses useDocuments(response.knowledge_base_id) with the
 * returned id to poll indexing status — no separate polling mechanism
 * needed here. */
export function useAttachFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { conversationId: string; file: File }) =>
      attachFileToConversation(input),
    onSuccess: (doc) => {
      queryClient.invalidateQueries({ queryKey: documentsKey(doc.knowledge_base_id) });
    },
  });
}

export function useDeleteDocument(knowledgeBaseId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteDocument({ knowledgeBaseId, id }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: documentsKey(knowledgeBaseId) });
    },
  });
}
