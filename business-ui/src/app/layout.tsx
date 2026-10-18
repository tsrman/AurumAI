import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getTranslations } from "next-intl/server";
import "./globals.css";
import { AuthProvider } from "@/providers/AuthProvider";
import { QueryProvider } from "@/providers/QueryProvider";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { ThemeProvider } from "@/providers/ThemeProvider";
import { AppShell } from "@/components/aurum/AppShell";
import { getServerRuntimeConfig, serializeRuntimeConfig } from "@/lib/runtime-config.server";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("metadata");
  return { title: "AurumAI", description: t("description") };
}

// Runtime config (API/Keycloak/admin URLs) must be resolved fresh per
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
      {/* The Aurum design system uses the local "Segoe UI"/Calibri stack
          (--font-sans in globals.css); the Inter import this template started
          with set an unused --font-inter variable and, worse, downloaded the
          font from Google Fonts during `next build` — an internet dependency
          that breaks air-gapped builds (docs/AIR_GAPPED.md). */}
      <body className="font-sans">
        <NextIntlClientProvider>
          <QueryProvider>
            <AuthProvider>
              <ThemeProvider>
                <AppShellProvider>
                  <AppShell>{children}</AppShell>
                </AppShellProvider>
              </ThemeProvider>
            </AuthProvider>
          </QueryProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
