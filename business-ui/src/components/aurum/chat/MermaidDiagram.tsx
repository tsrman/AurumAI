"use client";

import { useEffect, useId, useState } from "react";
import { useTheme } from "@/hooks/useTheme";
import { useTranslations } from "next-intl";

export interface MermaidDiagramProps {
  source: string;
}

// Renders a ```mermaid fenced block as an actual diagram instead of raw
// code. mermaid is imported dynamically (client-only, DOM-heavy) so it
// never touches the SSR bundle. securityLevel "strict" keeps mermaid's own
// DOMPurify sanitization on — diagram source ultimately comes from LLM
// output, which is not fully trusted input.
export function MermaidDiagram({ source }: MermaidDiagramProps) {
  const t = useTranslations("chat.mermaid");
  const { theme } = useTheme();
  const reactId = useId().replace(/[^a-zA-Z0-9]/g, "");
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const trimmed = source.trim();

  useEffect(() => {
    let cancelled = false;
    if (!trimmed) return;

    (async () => {
      try {
        const { default: mermaid } = await import("mermaid");
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: theme === "dark" ? "dark" : "default",
        });
        const { svg: rendered } = await mermaid.render(
          `mermaid-${reactId}`,
          trimmed
        );
        if (!cancelled) {
          setSvg(rendered);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setSvg(null);
          setError(err instanceof Error ? err.message : t("renderError"));
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [trimmed, theme, reactId, t]);

  if (error) {
    return (
      <div className="space-y-1.5">
        <pre className="max-h-96 overflow-auto rounded-card bg-[#1e1e1e] p-4">
          <code className="font-mono text-sm text-white/90">{source}</code>
        </pre>
        <p className="text-xs text-aurum-err-fg">
          {t("failed", { error })}
        </p>
      </div>
    );
  }

  if (!svg) {
    return (
      <div
        role="status"
        aria-live="polite"
        className="flex items-center justify-center rounded-card border border-aurum-border bg-aurum-surface p-6 text-sm text-aurum-text-3"
      >
        {t("rendering")}
      </div>
    );
  }

  return (
    <div
      className="overflow-x-auto rounded-card border border-aurum-border bg-aurum-surface p-4"
      // mermaid sanitizes this via DOMPurify under securityLevel "strict".
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
