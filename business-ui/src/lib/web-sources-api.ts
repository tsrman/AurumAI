import { api } from "./api";
import type { ApiResponse } from "@/types/api";
import type { Source, WebSource } from "@/types/aurum";
import type { MessageResponse } from "@/types/chat";

export interface WebSourceResponse {
  sourceId: string;
  kind: "search" | "read";
  requestedUrl: string | null;
  finalUrl: string;
  title: string | null;
  contentType: string | null;
  snippet: string | null;
  publishedAt: string | null;
  retrievedAt: string;
  truncated: boolean;
  pageCount: number | null;
}

export function safeExternalWebUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
      parsed.username ||
      parsed.password
    ) {
      return null;
    }
    return parsed.href;
  } catch {
    return null;
  }
}

export async function getWebSources(
  conversationId: string,
  turnId: string
): Promise<WebSourceResponse[]> {
  const response = await api
    .get(
      `conversations/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}/sources`
    )
    .json<ApiResponse<WebSourceResponse[]>>();
  return response.data;
}

export function sourcesForMessage(
  acceptedSourceIds: string[],
  unsupportedSourceIds: string[],
  availableSources: WebSourceResponse[]
): Source[] {
  const byId = new Map(availableSources.map((source) => [source.sourceId, source]));
  const seen = new Set<string>();
  const result: Source[] = [];

  for (const sourceId of acceptedSourceIds) {
    if (seen.has(sourceId)) continue;
    seen.add(sourceId);
    const source = byId.get(sourceId);
    if (!source) continue;
    const url = safeExternalWebUrl(source.finalUrl);
    if (!url) continue;
    const mapped: WebSource = {
      kind: "web",
      index: result.length + 1,
      sourceId,
      url,
      title: source.title?.trim() || url,
      snippet: source.snippet,
      retrievedAt: source.retrievedAt,
      publishedAt: source.publishedAt,
      truncated: source.truncated,
    };
    result.push(mapped);
  }

  for (const sourceId of unsupportedSourceIds) {
    if (!sourceId || seen.has(sourceId)) continue;
    seen.add(sourceId);
    result.push({
      kind: "unsupported",
      index: result.length + 1,
      sourceId,
    });
  }

  return result;
}

export async function loadSourcesForMessage(
  conversationId: string,
  message: MessageResponse
): Promise<Source[]> {
  if (message.role !== "assistant") return [];
  const accepted = message.sourceIds ?? [];
  const unsupported = message.unsupportedSourceIds ?? [];
  if (accepted.length === 0) {
    return sourcesForMessage([], unsupported, []);
  }
  if (!message.turnId) {
    return sourcesForMessage([], unsupported, []);
  }

  try {
    const available = await getWebSources(conversationId, message.turnId);
    return sourcesForMessage(accepted, unsupported, available);
  } catch {
    // Source metadata is fail-closed: a failed or unauthorized lookup must
    // not turn model-provided IDs/URLs into clickable citations.
    return sourcesForMessage([], unsupported, []);
  }
}
