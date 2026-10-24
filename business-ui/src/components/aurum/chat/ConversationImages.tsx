"use client";

import { useEffect, useState } from "react";
import { Download, ImageOff, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useAppShell } from "@/hooks/useAppShell";
import { getConversationImageBlob } from "@/lib/images-api";

function ConversationImage({
  conversationId,
  imageId,
  index,
}: {
  conversationId: string;
  imageId: string;
  index: number;
}) {
  const t = useTranslations("chat.images");
  const [state, setState] = useState<
    { status: "loading" } | { status: "ready"; url: string } | { status: "failed" }
  >({ status: "loading" });

  useEffect(() => {
    let revoked = false;
    let objectUrl: string | null = null;
    getConversationImageBlob(conversationId, imageId)
      .then((blob) => {
        if (revoked) return;
        objectUrl = URL.createObjectURL(blob);
        setState({ status: "ready", url: objectUrl });
      })
      .catch(() => {
        if (!revoked) setState({ status: "failed" });
      });
    return () => {
      revoked = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [conversationId, imageId]);

  const label = t("alt", { index: index + 1 });
  if (state.status === "loading") {
    return (
      <div
        role="status"
        aria-label={t("loading")}
        className="flex aspect-square w-full items-center justify-center rounded-card border border-aurum-border bg-aurum-surface-2 text-aurum-text-3"
      >
        <Loader2 className="h-6 w-6 animate-spin" aria-hidden="true" />
      </div>
    );
  }
  if (state.status === "failed") {
    return (
      <div className="flex aspect-square w-full flex-col items-center justify-center gap-2 rounded-card border border-dashed border-aurum-border bg-aurum-surface-2 p-4 text-center text-xs text-aurum-text-3">
        <ImageOff className="h-6 w-6" aria-hidden="true" />
        {t("unavailable")}
      </div>
    );
  }
  return (
    <figure className="group relative overflow-hidden rounded-card border border-aurum-border bg-aurum-surface-2">
      <a href={state.url} target="_blank" rel="noopener noreferrer" aria-label={t("open", { index: index + 1 })}>
        {/* eslint-disable-next-line @next/next/no-img-element -- local blob URL, not an optimizable asset */}
        <img src={state.url} alt={label} className="block h-auto w-full" />
      </a>
      <a
        href={state.url}
        download={`image-${imageId}`}
        aria-label={t("download", { index: index + 1 })}
        className="absolute right-2 top-2 inline-flex h-8 w-8 items-center justify-center rounded-btn bg-aurum-surface/90 text-aurum-text-2 opacity-0 shadow-card transition hover:text-aurum-text group-hover:opacity-100 focus:opacity-100"
      >
        <Download className="h-4 w-4" aria-hidden="true" />
      </a>
    </figure>
  );
}

/** Images an assistant answer produced, fetched with the user's session. */
export function ConversationImages({ imageIds }: { imageIds: string[] }) {
  const t = useTranslations("chat.images");
  const { activeConversationId } = useAppShell();
  if (!activeConversationId || imageIds.length === 0) return null;
  return (
    <div
      aria-label={t("label")}
      role="group"
      className={imageIds.length === 1 ? "max-w-md" : "grid max-w-2xl grid-cols-2 gap-3"}
    >
      {imageIds.map((imageId, index) => (
        <ConversationImage
          key={imageId}
          conversationId={activeConversationId}
          imageId={imageId}
          index={index}
        />
      ))}
    </div>
  );
}
