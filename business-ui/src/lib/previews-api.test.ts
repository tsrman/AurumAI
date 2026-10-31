import { describe, expect, it } from "vitest";
import { previewsFrom } from "./previews-api";

const valid = { previewId: "0b7c5a4e-1d2f-4a3b-9c8d-7e6f5a4b3c2d", port: 5173 };

describe("previewsFrom", () => {
  it("keeps only well-formed previews from an untrusted payload", () => {
    expect(
      previewsFrom([
        valid,
        { ...valid, previewId: "javascript:alert(1)" },
        { ...valid, port: 80 },
        { ...valid, port: 5173.5 },
        { ...valid, port: "5173" },
        null,
      ])
    ).toEqual([valid]);
    expect(previewsFrom(undefined)).toEqual([]);
  });
});
