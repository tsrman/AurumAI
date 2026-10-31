import { api } from "./api";

/** An app the answer's Hermes Agent shared from its sandbox (business-backend's SharedPreview). */
export interface SharedPreview {
  previewId: string;
  port: number;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Keep only well-formed entries from an untrusted history payload. */
export function previewsFrom(value: unknown): SharedPreview[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): SharedPreview[] => {
    if (typeof item !== "object" || item === null) return [];
    const { previewId, port } = item as Record<string, unknown>;
    if (typeof previewId !== "string" || !UUID.test(previewId)) return [];
    if (typeof port !== "number" || !Number.isInteger(port) || port < 1024 || port > 65535) return [];
    return [{ previewId, port }];
  });
}

/** The URL that opens the preview once: its own origin and a one-time ticket. */
export async function previewTicketUrl(conversationId: string, previewId: string): Promise<string> {
  const response = await api
    .post(
      `conversations/${encodeURIComponent(conversationId)}/previews/${encodeURIComponent(previewId)}/ticket`,
      { timeout: 30_000 }
    )
    .json<{ url: string }>();
  return response.url;
}
