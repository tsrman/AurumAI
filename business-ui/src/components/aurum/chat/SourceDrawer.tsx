"use client";

import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import { IconButton } from "@/components/primitives/IconButton";
import { useAppShell } from "@/hooks/useAppShell";
import { safeExternalWebUrl } from "@/lib/web-sources-api";
import { useLocale, useTranslations } from "next-intl";

function formatSourceDate(value: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, { timeZone: "UTC" }).format(
    new Date(value)
  );
}

export function SourceDrawer() {
  const t = useTranslations("chat.sourceDrawer");
  const locale = useLocale();
  const { drawerSource, setDrawerSource } = useAppShell();
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (drawerSource) {
      closeButtonRef.current?.focus();
    }
  }, [drawerSource]);

  useEffect(() => {
    if (!drawerSource) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setDrawerSource(null);
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [drawerSource, setDrawerSource]);

  if (!drawerSource) return null;

  const titleId = "source-drawer-title";
  const isWeb = drawerSource.kind === "web";
  const isUnsupported = drawerSource.kind === "unsupported";
  const title = isWeb
    ? drawerSource.title
    : isUnsupported
      ? t("unverifiedTitle")
      : drawerSource.doc;
  const subtitle = isWeb
    ? t("webSource")
    : isUnsupported
      ? `ID: ${drawerSource.sourceId}`
      : drawerSource.section;
  const safeUrl = isWeb ? safeExternalWebUrl(drawerSource.url) : null;

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-black/20"
        onClick={() => setDrawerSource(null)}
        aria-hidden="true"
      />
      <aside
        className="fixed right-0 top-0 z-50 flex h-full w-full max-w-md flex-col border-l border-aurum-border bg-aurum-surface shadow-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <div className="flex items-center justify-between border-b border-aurum-border-soft px-5 py-4">
          <div>
            <h3
              id={titleId}
              className="text-base font-semibold text-aurum-text"
            >
              {title}
            </h3>
            <p className="text-sm text-aurum-text-3">
              {subtitle}
            </p>
          </div>
          <IconButton
            ref={closeButtonRef}
            aria-label={t("close")}
            onClick={() => setDrawerSource(null)}
          >
            <X className="h-5 w-5" />
          </IconButton>
        </div>

        {isUnsupported ? (
          <div className="flex-1 overflow-auto p-5">
            <p className="rounded-card border border-aurum-border-soft bg-aurum-surface-2 p-4 text-sm leading-relaxed text-aurum-text-2">
              {t("unverifiedNotice")}
            </p>
          </div>
        ) : isWeb ? (
          <div className="flex-1 space-y-5 overflow-auto p-5">
            <div className="rounded-card border border-aurum-border-soft bg-aurum-surface-2 p-4 text-sm text-aurum-text-2">
              {drawerSource.publishedAt && (
                <p>{t("published", { date: formatSourceDate(drawerSource.publishedAt, locale) })}</p>
              )}
              <p>{t("retrieved", { date: formatSourceDate(drawerSource.retrievedAt, locale) })}</p>
              {drawerSource.truncated && (
                <p className="mt-2 font-medium text-aurum-text">{t("truncated")}</p>
              )}
            </div>
            {drawerSource.snippet && (
              <div>
                <p className="mb-2 text-sm font-medium text-aurum-text-2">
                  {t("fragment")}
                </p>
                <p className="whitespace-pre-wrap rounded-card border border-aurum-border-soft bg-aurum-surface-2 p-4 text-base leading-relaxed text-aurum-text">
                  {drawerSource.snippet}
                </p>
              </div>
            )}
            {safeUrl && (
              <a
                href={safeUrl}
                target="_blank"
                rel="noopener noreferrer"
                referrerPolicy="no-referrer"
                className="inline-flex text-sm font-medium text-aurum-accent hover:underline"
              >
                {t("open")}
              </a>
            )}
          </div>
        ) : (
          <div className="flex-1 space-y-5 overflow-auto p-5">
            <div className="rounded-card border border-aurum-border-soft bg-aurum-surface-2 p-4">
              <p className="text-sm font-medium text-aurum-text-2">
                {t("knowledgeBase")}
              </p>
              <p className="mt-1 text-base text-aurum-text">
                {drawerSource.knowledgeBase}
              </p>
            </div>

            <div className="rounded-card border border-aurum-border-soft bg-aurum-surface-2 p-4">
              <p className="text-sm font-medium text-aurum-text-2">
                {t("relevance")}
              </p>
              <p className="mt-1 text-base text-aurum-text">
                {drawerSource.score}%
              </p>
            </div>

            <div>
              <p className="mb-2 text-sm font-medium text-aurum-text-2">
                {t("fragment")}
              </p>
              <p className="whitespace-pre-wrap rounded-card border border-aurum-border-soft bg-aurum-surface-2 p-4 text-base leading-relaxed text-aurum-text">
                {drawerSource.snippet}
              </p>
            </div>
          </div>
        )}
      </aside>
    </>
  );
}
