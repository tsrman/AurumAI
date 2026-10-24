import { imageIdsFrom } from "@/lib/images-api";

test("keeps only well-formed image ids from untrusted payloads", () => {
  const id = "55555555-5555-4555-8555-555555555555";
  expect(imageIdsFrom([id, "../etc/passwd", 7, "https://cdn/a.png"])).toEqual([id]);
  expect(imageIdsFrom(undefined)).toEqual([]);
  expect(imageIdsFrom({ imageIds: [id] })).toEqual([]);
});
