import { api } from "./api";

/** Image bytes for one conversation image, through the authenticated API. */
export async function getConversationImageBlob(
  conversationId: string,
  imageId: string
): Promise<Blob> {
  return api
    .get(
      `conversations/${encodeURIComponent(conversationId)}/images/${encodeURIComponent(imageId)}/content`,
      { timeout: 60000 }
    )
    .blob();
}

/** Keep only well-formed ids from an untrusted SSE or history payload. */
export function imageIdsFrom(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is string =>
      typeof item === "string" &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(item)
  );
}
