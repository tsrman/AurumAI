import { api } from "./api";

/** A file an agent attached to its answer (business-backend's AttachedFile). */
export interface AttachedFile {
  fileId: string;
  name: string;
  contentType: string;
  sizeBytes: number;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Keep only well-formed entries from an untrusted history payload. */
export function filesFrom(value: unknown): AttachedFile[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): AttachedFile[] => {
    if (typeof item !== "object" || item === null) return [];
    const { fileId, name, contentType, sizeBytes } = item as Record<string, unknown>;
    if (
      typeof fileId !== "string" ||
      !UUID.test(fileId) ||
      typeof name !== "string" ||
      name.length === 0 ||
      name.length > 255 ||
      typeof contentType !== "string" ||
      typeof sizeBytes !== "number" ||
      !Number.isFinite(sizeBytes) ||
      sizeBytes < 0
    ) {
      return [];
    }
    return [{ fileId, name, contentType, sizeBytes }];
  });
}

/** The file's bytes, through the authenticated API. */
export async function getConversationFileBlob(conversationId: string, fileId: string): Promise<Blob> {
  return api
    .get(
      `conversations/${encodeURIComponent(conversationId)}/files/${encodeURIComponent(fileId)}/content`,
      { timeout: 120_000 }
    )
    .blob();
}
