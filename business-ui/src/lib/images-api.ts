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

export interface UploadedImage {
  imageId: string;
  contentType: string;
  width: number | null;
  height: number | null;
}

/** Store an image for the user's next message in this conversation. */
export async function uploadConversationImage(
  conversationId: string,
  file: File
): Promise<UploadedImage> {
  const form = new FormData();
  form.append("file", file);
  const response = await api
    .post(`conversations/${encodeURIComponent(conversationId)}/images`, {
      body: form,
      timeout: 120_000,
    })
    .json<{ data: UploadedImage }>();
  return response.data;
}

/** Discard an upload the user removed before sending it. */
export async function deleteConversationImage(conversationId: string, imageId: string): Promise<void> {
  await api.delete(
    `conversations/${encodeURIComponent(conversationId)}/images/${encodeURIComponent(imageId)}`
  );
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
