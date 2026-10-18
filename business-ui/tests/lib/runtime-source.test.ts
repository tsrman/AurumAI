import { afterEach, expect, test, vi } from "vitest";
import { getServerRuntimeConfig, serializeRuntimeConfig } from "@/lib/runtime-config.server";

afterEach(() => vi.unstubAllEnvs());

test("source URL is resolved again for each server request", () => {
  vi.stubEnv("RUNTIME_SOURCE_CODE_URL", "https://example.org/first/source");
  expect(getServerRuntimeConfig().sourceCodeUrl).toBe("https://example.org/first/source");
  vi.stubEnv("RUNTIME_SOURCE_CODE_URL", "https://example.org/second/source");
  expect(getServerRuntimeConfig().sourceCodeUrl).toBe("https://example.org/second/source");
});

test.each(["javascript:alert(1)", "file:///private/source", "https://user:password@example.org/source"])(
  "unsafe or credential-bearing source URL is omitted: %s", (url) => {
    vi.stubEnv("RUNTIME_SOURCE_CODE_URL", url);
    expect(getServerRuntimeConfig().sourceCodeUrl).toBe("");
  },
);

test("runtime injection never permits a source URL to close the script element", () => {
  vi.stubEnv("RUNTIME_SOURCE_CODE_URL", "https://example.org/</script>");
  expect(serializeRuntimeConfig(getServerRuntimeConfig())).not.toContain("</script>");
});
