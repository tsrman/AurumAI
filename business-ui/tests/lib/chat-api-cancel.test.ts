import { describe, it, expect, vi, beforeEach } from "vitest";
import { cancelChat } from "@/lib/chat-api";

describe("cancelChat", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.restoreAllMocks();
    Object.defineProperty(globalThis, "window", {
      value: { keycloak: { token: "test-token" } },
      writable: true,
      configurable: true,
    });
  });

  it("posts to /chat/{conversation_id}/cancel", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 204,
      text: vi.fn().mockResolvedValue(""),
    } as unknown as Response);

    await cancelChat("conv-123");

    expect(global.fetch).toHaveBeenCalledWith(
      "http://localhost:8000/v1/chat/conv-123/cancel",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          Authorization: "Bearer test-token",
        }),
      })
    );
  });

  it("throws on non-ok response", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      text: vi.fn().mockResolvedValue("Forbidden"),
    } as unknown as Response);

    await expect(cancelChat("conv-123")).rejects.toThrow("Forbidden");
  });

  it("refreshes a stale token on 401 and retries once", async () => {
    // Regression: AuthProvider's periodic token refresh used to update
    // the underlying Keycloak instance but never wrote the new token back
    // to window.keycloak.token, which every fetch() call in this file
    // reads directly — so a token that had genuinely expired (e.g. after
    // leaving the tab idle) surfaced as a bare 401 with no retry.
    const updateToken = vi.fn().mockImplementation(async () => {
      window.keycloak!.token = "refreshed-token";
      return true;
    });
    Object.defineProperty(globalThis, "window", {
      value: { keycloak: { token: "expired-token", updateToken } },
      writable: true,
      configurable: true,
    });

    global.fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        text: vi.fn().mockResolvedValue("Unauthorized"),
      } as unknown as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 204,
        text: vi.fn().mockResolvedValue(""),
      } as unknown as Response);

    await cancelChat("conv-123");

    expect(updateToken).toHaveBeenCalledWith(30);
    expect(global.fetch).toHaveBeenCalledTimes(2);
    expect(global.fetch).toHaveBeenLastCalledWith(
      "http://localhost:8000/v1/chat/conv-123/cancel",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer refreshed-token",
        }),
      })
    );
  });

  it("surfaces the 401 if the token refresh doesn't help", async () => {
    const updateToken = vi.fn().mockResolvedValue(false);
    Object.defineProperty(globalThis, "window", {
      value: { keycloak: { token: "expired-token", updateToken } },
      writable: true,
      configurable: true,
    });

    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      text: vi.fn().mockResolvedValue("Unauthorized"),
    } as unknown as Response);

    await expect(cancelChat("conv-123")).rejects.toThrow("Unauthorized");
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });
});
