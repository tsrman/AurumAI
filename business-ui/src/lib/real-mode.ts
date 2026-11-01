import { getRuntimeConfig } from "@/lib/runtime-config";

/** A signed-in browser session against a backend (not the unauthenticated demo). */
export function isRealMode(): boolean {
  return (
    typeof window !== "undefined" &&
    !!window.keycloak?.token &&
    !!getRuntimeConfig().apiUrl
  );
}
