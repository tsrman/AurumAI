import { api } from "@/lib/api";
import type { ApiResponse } from "@/types/api";

export interface KnowledgeBaseDto {
  id: string;
  name: string;
  description: string | null;
  canWrite: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface KnowledgeSearchResult {
  content: string;
  relevance_score?: number | null;
  metadata: {
    document_id?: string | null;
    document_title?: string | null;
  };
}

export interface KnowledgeSearchResponse {
  results: KnowledgeSearchResult[];
  warnings: string[];
}

export async function listKnowledgeBases(): Promise<KnowledgeBaseDto[]> {
  const response = await api
    .get("knowledge-bases")
    .json<ApiResponse<KnowledgeBaseDto[]>>();
  return response.data;
}

export async function createKnowledgeBase(input: {
  name: string;
  description: string;
}): Promise<KnowledgeBaseDto> {
  const response = await api
    .post("knowledge-bases", { json: input })
    .json<ApiResponse<KnowledgeBaseDto>>();
  return response.data;
}

export async function deleteKnowledgeBase(id: string): Promise<void> {
  await api.delete(`knowledge-bases/${id}`);
}

export async function searchKnowledgeBase(input: {
  knowledgeBaseId: string;
  query: string;
  limit: number;
}): Promise<KnowledgeSearchResponse> {
  return api
    .post(`knowledge-bases/${input.knowledgeBaseId}/search`, {
      json: { query: input.query, limit: input.limit },
    })
    .json<KnowledgeSearchResponse>();
}
