"use client";

import {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  ReactNode,
} from "react";
import Keycloak from "keycloak-js";
import { api } from "@/lib/api";
import { getRuntimeConfig } from "@/lib/runtime-config";
import { UserInfo } from "@/types/auth";

interface AuthContextValue {
  user: UserInfo | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: () => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [kc, setKc] = useState<Keycloak | null>(null);
  const [user, setUser] = useState<UserInfo | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    if (typeof window === "undefined") return;

    const { keycloakUrl, keycloakRealm: realm, keycloakClientId: clientId } = getRuntimeConfig();

    const instance = new Keycloak({
      url: keycloakUrl,
      realm,
      clientId,
    });

    // Declared in the outer effect's closure (not inside the .then() below)
    // so the effect's own cleanup function — the only one React actually
    // calls on unmount — can reach it. Returning a cleanup function from
    // the .then() callback instead (the previous shape here) is silently
    // discarded; the timer kept running past unmount (the security review,
    // same bug and fix as business-ui's AuthProvider.tsx).
    let cancelled = false;
    let refreshTimer: ReturnType<typeof setTimeout> | null = null;

    const REFRESH_INTERVAL_MS = 60_000;
    const MAX_BACKOFF_MS = 5 * 60_000;

    const scheduleRefresh = (delayMs: number) => {
      refreshTimer = setTimeout(() => {
        instance
          .updateToken(60)
          .then((refreshed) => {
            if (cancelled) return;
            if (refreshed && window.keycloak) {
              window.keycloak.token = instance.token;
              setToken(instance.token ?? null);
            }
            scheduleRefresh(REFRESH_INTERVAL_MS);
          })
          .catch(() => {
            if (cancelled) return;
            // keycloak-js only clears kc.refreshToken itself when the token
            // endpoint returns 400 (refresh token invalid/expired) — a real
            // auth failure that requires re-login. Any other failure
            // (network blip, gateway 5xx, Keycloak restart) leaves
            // refreshToken intact and must not force a logout the user
            // never asked for; back off and try again instead.
            if (!instance.refreshToken) {
              instance.logout({ redirectUri: window.location.origin });
              return;
            }
            scheduleRefresh(Math.min(delayMs * 2, MAX_BACKOFF_MS));
          });
      }, delayMs);
    };

    instance
      .init({
        onLoad: "check-sso",
        silentCheckSsoRedirectUri: `${window.location.origin}/silent-check-sso.html`,
        pkceMethod: "S256",
      })
      .then((authenticated) => {
        if (cancelled) return;
        if (authenticated) {
          const parsed = instance.tokenParsed as Record<string, unknown> | undefined;
          const accessToken = instance.token || "";
          setUser({
            userId: parsed?.sub as string,
            email: parsed?.email as string,
            fullName: parsed?.name as string,
            orgId: parsed?.org_id as string,
            orgName: parsed?.org_name as string,
            role: (parsed?.role as string) || "member",
            token: accessToken,
          });
          setToken(accessToken);
          window.keycloak = {
            token: accessToken,
            // keycloak-js refreshes `instance.token` in place, but nothing
            // else reads it directly — every other consumer (chat-api.ts,
            // api.ts) reads `window.keycloak.token`, so a refresh that
            // doesn't write back here is invisible to them and the token
            // goes stale the moment the original access token expires.
            updateToken: async (min) => {
              const refreshed = await instance.updateToken(min);
              if (window.keycloak) window.keycloak.token = instance.token;
              setToken(instance.token ?? null);
              return refreshed;
            },
          };
          // Sync user with backend (creates DB row if missing)
          api.get("auth/me").catch(() => null);

          scheduleRefresh(REFRESH_INTERVAL_MS);
        } else {
          setToken(null);
        }
      })
      .finally(() => {
        if (cancelled) return;
        setKc(instance);
        setIsLoading(false);
      });

    return () => {
      cancelled = true;
      if (refreshTimer) clearTimeout(refreshTimer);
    };
  }, []);

  const login = useCallback(() => {
    if (typeof window === "undefined") return;
    kc?.login({ redirectUri: window.location.href });
  }, [kc]);

  const logout = useCallback(() => {
    if (typeof window === "undefined") return;
    kc?.logout({ redirectUri: window.location.origin });
  }, [kc]);

  return (
    <AuthContext.Provider
      value={{
        user,
        token,
        isAuthenticated: !!user,
        isLoading,
        login,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
