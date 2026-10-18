export interface RuntimeConfig {
  sourceCodeUrl: string;
  apiUrl: string;
  keycloakUrl: string;
  keycloakRealm: string;
  keycloakClientId: string;
  /** business-ui chat link, the reverse of business-ui adminUrl. */
  appUrl: string;
}

export const DEFAULT_RUNTIME_CONFIG: RuntimeConfig = {
  sourceCodeUrl: "",
  apiUrl: "http://localhost:8001/v1",
  keycloakUrl: "http://localhost:8080",
  keycloakRealm: "aurum-ai",
  keycloakClientId: "admin-ui",
  appUrl: "http://localhost:3000",
};

declare global {
  interface Window {
    __RUNTIME_CONFIG__?: RuntimeConfig;
  }
}
