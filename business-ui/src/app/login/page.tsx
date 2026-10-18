"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Key } from "lucide-react";
import { useAuth } from "@/providers/AuthProvider";
import { useTranslations } from "next-intl";

export default function LoginPage() {
  const t = useTranslations("login");
  const aboutT = useTranslations("about");
  const { isAuthenticated, isLoading, login } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && isAuthenticated) {
      router.push("/chat");
    }
  }, [isLoading, isAuthenticated, router]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-aurum-bg px-4 text-aurum-text">
      <div className="w-full max-w-sm rounded-card border border-aurum-border bg-aurum-surface p-8 shadow-card">
        <div className="mb-6 flex flex-col items-center gap-3">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-aurum-accent-soft text-aurum-accent-ink">
            <Key className="h-6 w-6" />
          </div>
          <h1 className="text-center text-2xl font-semibold">AurumAI</h1>
          <p className="text-center text-sm text-aurum-text-2">
            {t("subtitle")}
          </p>
        </div>

        <a href="/about" className="mb-4 block text-center text-sm underline">{aboutT("linkFull")}</a>

        <button
          onClick={login}
          disabled={isLoading}
          aria-busy={isLoading}
          aria-live="polite"
          className="flex w-full items-center justify-center gap-2 rounded-btn bg-aurum-accent px-4 py-2.5 font-medium text-aurum-accent-ink transition hover:bg-aurum-accent/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-aurum-accent focus-visible:ring-offset-2 focus-visible:ring-offset-aurum-bg disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Key className="h-4 w-4" aria-hidden="true" />
          {isLoading ? t("signingIn") : t("signIn")}
        </button>
      </div>
    </div>
  );
}
