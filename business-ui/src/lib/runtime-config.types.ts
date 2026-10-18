export interface RuntimeConfig {
  sourceCodeUrl: string;
  apiUrl: string;
  keycloakUrl: string;
  keycloakRealm: string;
  keycloakClientId: string;
  /** admin-ui — separate app, separate origin. */
  adminUrl: string;
}

export const DEFAULT_RUNTIME_CONFIG: RuntimeConfig = {
  sourceCodeUrl: "",
  apiUrl: "http://localhost:8000/v1",
  keycloakUrl: "http://localhost:8080",
  keycloakRealm: "aurum-ai",
  keycloakClientId: "business-ui",
  adminUrl: "http://localhost:3100",
};

declare global {
  interface Window {
    __RUNTIME_CONFIG__?: RuntimeConfig;
  }
}
