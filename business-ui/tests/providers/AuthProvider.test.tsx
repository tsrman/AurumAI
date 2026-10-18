import { render, screen, waitFor, act } from "@testing-library/react";
import { vi } from "vitest";
import { AuthProvider, useAuth } from "@/providers/AuthProvider";

const { apiGet, getMe } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  getMe: vi.fn(),
}));

// Mirrors real keycloak-js: updateToken() mutates `this.token` in place —
// nothing else about the instance changes shape. Any code that wants the
// fresh token has to re-read `instance.token` after the call, which is
// exactly the step AuthProvider used to skip for window.keycloak.token.
const MockKeycloak = vi.hoisted(() =>
  // A regular `function` expression, not an arrow function: AuthProvider
  // calls `new Keycloak(...)`, and vitest's mock proxy now actually invokes
  // the implementation via `new` — arrow functions are never constructible,
  // so this must stay a `function` (same reasoning at every
  // mockImplementationOnce below).
  vi.fn().mockImplementation(function () {
    return {
      token: "initial-token",
      tokenParsed: { sub: "user-1", email: "u@example.com", role: "member" },
      init: () => Promise.resolve(true),
      login: () => {},
      logout: () => {},
      updateToken(this: { token: string }) {
        this.token = "refreshed-token";
        return Promise.resolve(true);
      },
    };
  })
);

vi.mock("keycloak-js", () => ({
  default: MockKeycloak,
}));

vi.mock("@/lib/api", () => ({
  api: { get: apiGet },
  getMe,
}));

function TestComponent() {
  const {
    token,
    isAuthenticated,
    authorizationReady,
    hasCapability,
  } = useAuth();
  return (
    <div>
      <span data-testid="token">{token}</span>
      <span data-testid="authed">{String(isAuthenticated)}</span>
      <span data-testid="authorization-ready">{String(authorizationReady)}</span>
      <span data-testid="can-create-knowledge">
        {String(hasCapability("workspace.knowledge.create"))}
      </span>
    </div>
  );
}

describe("AuthProvider", () => {
  beforeEach(() => {
    delete (window as { keycloak?: unknown }).keycloak;
    apiGet.mockReset().mockResolvedValue(undefined);
    getMe.mockReset().mockResolvedValue({
      data: {
        user_id: "user-1",
        email: "u@example.com",
        full_name: "User",
        org_id: "org-1",
        org_name: "Org",
        role: "member",
        capabilities: ["workspace.knowledge.create"],
      },
      meta: { requestId: "req", timestamp: "2026-09-03T00:00:00Z" },
    });
  });

  it("exposes capabilities returned by the live auth endpoint", async () => {
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("authorization-ready")).toHaveTextContent("true");
    });
    expect(screen.getByTestId("can-create-knowledge")).toHaveTextContent("true");
    expect(getMe).toHaveBeenCalledTimes(1);
  });

  it("fails closed when the live capability request fails", async () => {
    getMe.mockRejectedValueOnce(new Error("backend unavailable"));

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("authorization-ready")).toHaveTextContent("true");
    });
    expect(screen.getByTestId("can-create-knowledge")).toHaveTextContent("false");
  });

  it("exposes window.keycloak.token after a successful init", async () => {
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId("authed")).toHaveTextContent("true");
    });
    expect(window.keycloak?.token).toBe("initial-token");
  });

  it("writes the refreshed token back to window.keycloak.token when window.keycloak.updateToken() is called", async () => {
    // Regression: instance.updateToken() refreshes keycloak-js's own
    // internal state, but every other module (chat-api.ts, api.ts) reads
    // window.keycloak.token directly, not the Keycloak instance. If that
    // global is never updated after a refresh, it stays pinned to the
    // token from page load — and once the real token expires, every
    // request made afterwards 401s. This surfaced as "sending a message
    // after being idle returns 401".
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(window.keycloak?.token).toBe("initial-token");
    });

    let refreshed = false;
    await act(async () => {
      refreshed = (await window.keycloak?.updateToken(30)) ?? false;
    });

    expect(refreshed).toBe(true);
    expect(window.keycloak?.token).toBe("refreshed-token");
    expect(screen.getByTestId("token")).toHaveTextContent("refreshed-token");
  });

  it("the periodic background refresh also writes the new token back to window.keycloak.token", async () => {
    vi.useFakeTimers();
    try {
      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      // Let the init() promise resolve before advancing timers.
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(window.keycloak?.token).toBe("initial-token");

      // AuthProvider re-checks/refreshes every 60s.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });

      expect(window.keycloak?.token).toBe("refreshed-token");
    } finally {
      vi.useRealTimers();
    }
  });

  it("a transient updateToken failure does not force a logout", async () => {
    const logoutSpy = vi.fn();
    MockKeycloak.mockImplementationOnce(function () {
      return {
        token: "initial-token",
        refreshToken: "refresh-token",
        tokenParsed: { sub: "user-1", email: "u@example.com", role: "member" },
        init: () => Promise.resolve(true),
        login: () => {},
        logout: logoutSpy,
        // Network blip / gateway 5xx: keycloak-js leaves refreshToken intact
        // on any failure other than a 400 from the token endpoint.
        updateToken: () => Promise.reject(new Error("network error")),
      };
    });

    vi.useFakeTimers();
    try {
      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });

      expect(logoutSpy).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("logs out with redirectUri once the refresh token itself is invalidated", async () => {
    const logoutSpy = vi.fn();
    const instanceState: { refreshToken: string | undefined } = {
      refreshToken: "refresh-token",
    };
    MockKeycloak.mockImplementationOnce(function () {
      return {
        token: "initial-token",
        get refreshToken() {
          return instanceState.refreshToken;
        },
        tokenParsed: { sub: "user-1", email: "u@example.com", role: "member" },
        init: () => Promise.resolve(true),
        login: () => {},
        logout: logoutSpy,
        // A real 400 from the token endpoint: keycloak-js clears its own
        // refreshToken before the caller's .catch() runs.
        updateToken: () => {
          instanceState.refreshToken = undefined;
          return Promise.reject(new Error("invalid_grant"));
        },
      };
    });

    vi.useFakeTimers();
    try {
      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });

      expect(logoutSpy).toHaveBeenCalledWith({ redirectUri: window.location.origin });
    } finally {
      vi.useRealTimers();
    }
  });

  it("unmounting clears the pending refresh timer instead of leaking it", async () => {
    const updateTokenSpy = vi.fn().mockResolvedValue(true);
    MockKeycloak.mockImplementationOnce(function () {
      return {
        token: "initial-token",
        refreshToken: "refresh-token",
        tokenParsed: { sub: "user-1", email: "u@example.com", role: "member" },
        init: () => Promise.resolve(true),
        login: () => {},
        logout: () => {},
        updateToken: updateTokenSpy,
      };
    });

    vi.useFakeTimers();
    try {
      const { unmount } = render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });

      unmount();

      // Previously the cleanup function was returned from inside the
      // .then() callback, not the effect itself, so React never called it
      // and this timer kept firing after unmount.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });

      expect(updateTokenSpy).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
