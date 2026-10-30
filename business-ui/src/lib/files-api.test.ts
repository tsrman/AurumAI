import { describe, expect, it } from "vitest";
import { filesFrom } from "./files-api";

const valid = {
  fileId: "0b7c5a4e-1d2f-4a3b-9c8d-7e6f5a4b3c2d",
  name: "report.csv",
  contentType: "text/csv",
  sizeBytes: 21,
};

describe("filesFrom", () => {
  it("keeps only well-formed files from an untrusted payload", () => {
    expect(
      filesFrom([
        valid,
        { ...valid, fileId: "../../admin" },
        { ...valid, name: "" },
        { ...valid, name: "x".repeat(256) },
        { ...valid, sizeBytes: "21" },
        { ...valid, sizeBytes: -1 },
        null,
        "report.csv",
      ])
    ).toEqual([valid]);
    expect(filesFrom(undefined)).toEqual([]);
    expect(filesFrom({ files: [valid] })).toEqual([]);
  });
});
