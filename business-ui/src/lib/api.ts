import ky from "ky";
import { ApiResponse } from "@/types/api";
import { getRuntimeConfig } from "@/lib/runtime-config";

declare global {
  interface Window {
    keycloak?: {
      token?: string;
      updateToken: (min: number) => Promise<boolean>;
    };
  }
}

export interface Me {
  user_id: string;
  email?: string | null;
  full_name?: string | null;
  org_id?: string | null;
  org_name?: string | null;
  role: string;
  capabilities: string[];
}

export interface Model {
  id: string;
  object?: string;
  vision?: boolean;
}

export const api = ky.create({
  prefixUrl: getRuntimeConfig().apiUrl,
  timeout: 30000,
  retry: {
    limit: 3,
    // POST/PATCH are not idempotent here — retrying a
    // POST that committed before the response came back (network drop,
    // upstream 5xx after success) creates a duplicate (e.g. POST
    // /v1/skills, /v1/skills/code — no unique constraint on name), and
    // POST /v1/skills/discover-mcp-tools returns 502 as an ordinary "could
    // not connect" result, not a transient gateway failure — retrying it
    // just triples the connection attempts and delays showing the real
    // error. GET/PUT are ky's own default retry-safe set.
    methods: ["get", "put"],
    statusCodes: [429, 500, 502, 503],
    backoffLimit: 8000,
  },
  hooks: {
    beforeRequest: [
      (request) => {
        const token = window.keycloak?.token;
        if (token) request.headers.set("Authorization", `Bearer ${token}`);
      },
    ],
    afterResponse: [
      async (request, _options, response) => {
        if (response.status === 401) {
          // an expired/invalid refresh token makes
          // keycloak-js's own updateToken() reject — unguarded, that
          // exception replaced the original 401 HTTPError for every caller
          // that checks response.status (see lib/errors.ts), with
          // keycloak's own error instead. Fall through to the original 401
          // response on any refresh failure, same as when refreshed=false.
          try {
            const refreshed = await window.keycloak?.updateToken(30);
            if (refreshed) {
              request.headers.set("Authorization", `Bearer ${window.keycloak?.token}`);
              return ky(request);
            }
          } catch {
            // fall through to `return response` below
          }
        }
        return response;
      },
    ],
  },
});

export async function getMe() {
  return (await api.get("auth/me")).json<ApiResponse<Me>>();
}

export async function getModels() {
  return (await api.get("models")).json<ApiResponse<Model[]>>();
}
