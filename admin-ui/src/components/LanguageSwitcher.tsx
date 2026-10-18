"use client";

import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { LOCALE_COOKIE, LOCALES, type Locale } from "@/i18n/locale";
import { cn } from "@/lib/utils";

/** Each language is shown by its own name, independent of the active locale. */
function autonym(locale: Locale): string {
  const name = new Intl.DisplayNames([locale], { type: "language" }).of(locale) ?? locale;
  return name.charAt(0).toLocaleUpperCase(locale) + name.slice(1);
}

export function LanguageSwitcher({ className }: { className?: string }) {
  const t = useTranslations("common");
  const locale = useLocale();
  const router = useRouter();

  return (
    <select
      aria-label={t("language")}
      title={t("language")}
      value={locale}
      onChange={(event) => {
        // The server reads this cookie on every request; refresh re-renders
        // server components and messages in the chosen language.
        document.cookie = `${LOCALE_COOKIE}=${event.target.value}; path=/; max-age=31536000; samesite=lax`;
        router.refresh();
      }}
      className={cn(
        "rounded-btn border border-aurum-border bg-aurum-surface px-2 py-1 text-xs text-aurum-text-2",
        className
      )}
    >
      {LOCALES.map((option) => (
        <option key={option} value={option}>
          {autonym(option)}
        </option>
      ))}
    </select>
  );
}
