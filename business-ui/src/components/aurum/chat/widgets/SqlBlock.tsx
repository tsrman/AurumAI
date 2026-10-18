"use client";

import { useCallback } from "react";
import { Copy, Check } from "lucide-react";
import { useState } from "react";
import { IconButton } from "@/components/primitives/IconButton";
import { useTranslations } from "next-intl";

export interface SqlBlockProps {
  sql: string;
}

export function SqlBlock({ sql }: SqlBlockProps) {
  const t = useTranslations("chat.sql");
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    if (!sql) return;
    void navigator.clipboard.writeText(sql).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }, [sql]);

  return (
    <div className="overflow-hidden rounded-card bg-[#1e1e1e]" aria-label={t("label")}>
      <div className="flex items-center justify-between border-b border-white/10 px-3 py-2">
        <span className="text-xs font-medium text-white/60">SQL</span>
        <IconButton
          bare
          aria-label={t("copy")}
          onClick={handleCopy}
          className="text-white/60 hover:bg-white/10 hover:text-white"
        >
          {copied ? (
            <Check className="h-4 w-4" />
          ) : (
            <Copy className="h-4 w-4" />
          )}
        </IconButton>
      </div>
      <pre className="max-h-96 overflow-auto p-4 font-mono text-sm text-white/90">
        <code>{sql}</code>
      </pre>
    </div>
  );
}
