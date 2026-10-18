import { api } from "@/lib/api";
import { ApiResponse } from "@/types/api";

export interface DocumentDto {
  id: string;
  knowledge_base_id: string;
  title: string;
  /** Present only for documents uploaded from a file. */
  source_filename?: string | null;
  status: string;
  chunk_count: number;
  error?: string | null;
  created_at: string;
  updated_at: string;
}

export async function listDocuments(knowledgeBaseId: string): Promise<DocumentDto[]> {
  const response = await api
    .get("documents", {
      searchParams: { knowledge_base_id: knowledgeBaseId },
    })
    .json<ApiResponse<DocumentDto[]>>();
  return response.data;
}

export async function uploadDocument(input: {
  knowledgeBaseId: string;
  file: File;
}): Promise<DocumentDto> {
  const form = new FormData();
  form.append("knowledge_base_id", input.knowledgeBaseId);
  form.append("file", input.file);
  // Upload through the API: MinIO is loopback-only, so a direct presigned
  // URL would work only for browsers on the Docker host. Allow a larger
  // timeout than the standard request: parsing starts after the response,
  // but sending25MB over a slow connection can exceed30 seconds.
  const response = await api
    .post("documents/upload", { body: form, timeout: 120_000 })
    .json<ApiResponse<DocumentDto>>();
  return response.data;
}

export async function attachFileToConversation(input: {
  conversationId: string;
  file: File;
}): Promise<DocumentDto> {
  const form = new FormData();
  form.append("file", input.file);
  // Same shape as uploadDocument (documents/upload): the backend lazily
  // provisions a hidden, conversation-only knowledge base on the first
  // attachment and reuses it for every subsequent one — the response's
  // knowledge_base_id is how the composer polls status via useDocuments.
  const response = await api
    .post(`conversations/${input.conversationId}/attachments`, {
      body: form,
      timeout: 120_000,
    })
    .json<ApiResponse<DocumentDto>>();
  return response.data;
}

export async function deleteDocument(input: {
  id: string;
  knowledgeBaseId: string;
}): Promise<void> {
  await api.delete(`documents/${input.id}`, {
    searchParams: { knowledge_base_id: input.knowledgeBaseId },
  });
}
