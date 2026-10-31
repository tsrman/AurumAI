"use client";

import { useState } from "react";
import { ExternalLink, Loader2, MonitorPlay } from "lucide-react";
import { useTranslations } from "next-intl";
import { useAppShell } from "@/hooks/useAppShell";
import { previewTicketUrl, type SharedPreview } from "@/lib/previews-api";

function PreviewCard({ conversationId, preview }: { conversationId: string; preview: SharedPreview }) {
  const t = useTranslations("chat.previews");
  const [state, setState] = useState<"idle" | "opening" | "failed">("idle");

  async function open() {
    // Opened now, while the click still counts, so popup blockers allow it;
    // the preview's origin gets no handle on this page.
    const tab = window.open("about:blank", "_blank");
    if (tab) tab.opener = null;
    setState("opening");
    try {
      const url = await previewTicketUrl(conversationId, preview.previewId);
      if (tab) tab.location.href = url;
      else window.open(url, "_blank", "noopener,noreferrer");
      setState("idle");
    } catch {
      tab?.close();
      setState("failed");
    }
  }

  return (
    <div className="flex items-center gap-3 rounded-card border border-aurum-border bg-aurum-surface-2 px-3 py-2">
      <MonitorPlay className="h-5 w-5 shrink-0 text-aurum-text-3" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="text-sm text-aurum-text">{t("title")}</p>
        <p className="text-xs text-aurum-text-3">
          {state === "failed" ? <span role="alert">{t("failed")}</span> : t("hint", { port: preview.port })}
        </p>
      </div>
      <button
        type="button"
        onClick={open}
        disabled={state === "opening"}
        className="inline-flex items-center gap-1.5 rounded-btn border border-aurum-border px-2.5 py-1 text-sm text-aurum-text-2 transition hover:bg-aurum-surface hover:text-aurum-text disabled:opacity-60"
      >
        {state === "opening" ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        ) : (
          <ExternalLink className="h-4 w-4" aria-hidden="true" />
        )}
        {t("open")}
      </button>
    </div>
  );
}

/** Apps the answer's Hermes Agent shared from its sandbox, each opened at its own origin. */
export function ConversationPreviews({ previews }: { previews: SharedPreview[] }) {
  const t = useTranslations("chat.previews");
  const { activeConversationId } = useAppShell();
  if (!activeConversationId || previews.length === 0) return null;
  return (
    <div role="group" aria-label={t("label")} className="flex max-w-md flex-col gap-2">
      {previews.map((preview) => (
        <PreviewCard key={preview.previewId} conversationId={activeConversationId} preview={preview} />
      ))}
    </div>
  );
}
