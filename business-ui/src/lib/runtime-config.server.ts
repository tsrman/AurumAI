import { DEFAULT_RUNTIME_CONFIG, type RuntimeConfig } from "./runtime-config.types";

// RUNTIME_* on purpose, not NEXT_PUBLIC_*: Next.js inlines NEXT_PUBLIC_*
// values into the compiled output at `next build` regardless of whether the
// reading code is server- or client-only. An unprefixed name is never
// touched by that substitution, so this always does a real process.env
// lookup against the container's actual environment on every request —
// only Server Components/route handlers should import this module.
export function getServerRuntimeConfig(): RuntimeConfig {
  return {
    sourceCodeUrl: sourceCodeUrl(process.env.RUNTIME_SOURCE_CODE_URL),
    apiUrl: process.env.RUNTIME_API_URL || DEFAULT_RUNTIME_CONFIG.apiUrl,
    keycloakUrl: process.env.RUNTIME_KEYCLOAK_URL || DEFAULT_RUNTIME_CONFIG.keycloakUrl,
    keycloakRealm: process.env.RUNTIME_KEYCLOAK_REALM || DEFAULT_RUNTIME_CONFIG.keycloakRealm,
    keycloakClientId:
      process.env.RUNTIME_KEYCLOAK_CLIENT_ID || DEFAULT_RUNTIME_CONFIG.keycloakClientId,
    adminUrl: process.env.RUNTIME_ADMIN_URL || DEFAULT_RUNTIME_CONFIG.adminUrl,
  };
}

// Injected into the page as `window.__RUNTIME_CONFIG__ = <this>;` — escape
// "<" so a value can't close the surrounding <script> tag early.
export function serializeRuntimeConfig(config: RuntimeConfig): string {
  return JSON.stringify(config).replace(/</g, "\\u003c");
}

// Public source links must not execute code or disclose URL credentials.
function sourceCodeUrl(value: string | undefined): string {
  if (!value) return "";
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
  } catch {
    return "";
  }
}
