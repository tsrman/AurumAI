import { vi } from "vitest";
import { HTTPError } from "ky";
import { api } from "@/lib/api";

function make502() {
  return new Response("bad gateway", { status: 502 });
}

function make401() {
  return new Response("unauthorized", { status: 401 });
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(make502()));
  vi.stubGlobal("window", { keycloak: { token: "test-token" } });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("does not retry a POST that fails with 502 (not idempotent)", async () => {
  await expect(api.post("skills", { json: { name: "x" } })).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
});

test("does not retry a PATCH that fails with 502 (not idempotent)", async () => {
  await expect(api.patch("skills/1", { json: { name: "x" } })).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
});

test("still retries a GET that fails with 502 (safe to repeat)", async () => {
  vi.useFakeTimers();
  const resultPromise = api.get("skills").catch(() => "rejected");

  // ky's retry backoff runs on real setTimeout internally; fast-forward
  // through all 3 retry delays without slowing the test down.
  for (let i = 0; i < 3; i++) {
    await vi.advanceTimersByTimeAsync(10_000);
  }

  await resultPromise;
  expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(1);
});

test("surfaces the original 401 when the refresh token itself is invalid", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(make401()));
  vi.stubGlobal("window", {
    keycloak: {
      token: "test-token",
      // Mirrors a real keycloak-js rejection when the refresh token is
      // expired/invalid — must not replace the original 401 HTTPError.
      updateToken: vi.fn().mockRejectedValue(new Error("invalid_grant")),
    },
  });

  let caught: unknown;
  try {
    await api.get("skills");
  } catch (err) {
    caught = err;
  }

  expect(caught).toBeInstanceOf(HTTPError);
  expect((caught as HTTPError).response.status).toBe(401);
});
