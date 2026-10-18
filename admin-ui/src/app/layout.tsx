import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getTranslations } from "next-intl/server";
import "./globals.css";
import { AuthProvider } from "@/providers/AuthProvider";
import { QueryProvider } from "@/providers/QueryProvider";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { ThemeProvider } from "@/providers/ThemeProvider";
import { AuthGuard } from "@/components/AuthGuard";
import { getServerRuntimeConfig, serializeRuntimeConfig } from "@/lib/runtime-config.server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("metadata");
  return { title: t("title"), description: t("description") };
}

// Runtime config (API/Keycloak/app URLs) must be resolved fresh per
// request, not baked in at `next build` — see docs/ARCHITECTURE.md.
export const dynamic = "force-dynamic";

const themeScript = `(function () {
  try {
    var stored = localStorage.getItem('aurum-theme');
    if (stored === 'dark' || stored === 'light') {
      if (stored === 'dark') document.documentElement.classList.add('dark');
      return;
    }
  } catch (e) {
    // Ignore storage access errors.
  }
  try {
    if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
      document.documentElement.classList.add('dark');
    }
  } catch (e) {
    // Ignore matchMedia errors.
  }
})();`;

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const runtimeConfig = getServerRuntimeConfig();
  const locale = await getLocale();
  return (
    <html lang={locale} suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `window.__RUNTIME_CONFIG__ = ${serializeRuntimeConfig(runtimeConfig)};`,
          }}
        />
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="font-sans">
        <NextIntlClientProvider>
          <QueryProvider>
            <AuthProvider>
              <ThemeProvider>
                <AppShellProvider>
                  <AuthGuard>{children}</AuthGuard>
                </AppShellProvider>
              </ThemeProvider>
            </AuthProvider>
          </QueryProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
