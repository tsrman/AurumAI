import { DEFAULT_RUNTIME_CONFIG, type RuntimeConfig } from "./runtime-config.types";

export type { RuntimeConfig };

// Client-safe counterpart of runtime-config.server.ts. The root layout (a
// Server Component) injects the real, per-request config into
// `window.__RUNTIME_CONFIG__` before any other script runs, so this is
// always available by the time client code executes in the browser. Falls
// back to the same defaults for SSR of "use client" components (no window)
// and for tests (jsdom, nothing injected).
export function getRuntimeConfig(): RuntimeConfig {
  if (typeof window !== "undefined" && window.__RUNTIME_CONFIG__) {
    return window.__RUNTIME_CONFIG__;
  }
  return DEFAULT_RUNTIME_CONFIG;
}
