import { beforeEach, vi } from "vitest";

const post = vi.fn();
vi.mock("@/lib/api", () => ({ api: { post: (...args: unknown[]) => post(...args) } }));

import { imageIdsFrom, uploadConversationImage } from "@/lib/images-api";

beforeEach(() => post.mockReset());

test("uploads an image as multipart form data to its conversation", async () => {
  const uploaded = { imageId: "i-1", contentType: "image/png", width: 2, height: 2 };
  post.mockReturnValue({ json: async () => ({ data: uploaded }) });
  const file = new File([new Uint8Array([1])], "a.png", { type: "image/png" });

  await expect(uploadConversationImage("conv/1", file)).resolves.toEqual(uploaded);

  const [path, options] = post.mock.calls[0];
  expect(path).toBe("conversations/conv%2F1/images");
  expect((options.body as FormData).get("file")).toBe(file);
});

test("keeps only well-formed image ids from untrusted payloads", () => {
  const id = "55555555-5555-4555-8555-555555555555";
  expect(imageIdsFrom([id, "../etc/passwd", 7, "https://cdn/a.png"])).toEqual([id]);
  expect(imageIdsFrom(undefined)).toEqual([]);
  expect(imageIdsFrom({ imageIds: [id] })).toEqual([]);
});
