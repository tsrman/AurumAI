import * as React from "react";
import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

export type NoticeTone = "info" | "success" | "warning" | "error";

const TONES: Record<NoticeTone, { className: string; Icon: typeof Info }> = {
  info: { className: "border-aurum-border bg-aurum-surface-2 text-aurum-text-2", Icon: Info },
  success: { className: "border-transparent bg-aurum-ok-bg text-aurum-ok-fg", Icon: CheckCircle2 },
  warning: { className: "border-transparent bg-c-amber-bg text-c-amber-fg", Icon: AlertTriangle },
  error: { className: "border-transparent bg-aurum-err-bg text-aurum-err-fg", Icon: XCircle },
};

export interface NoticeProps {
  tone?: NoticeTone;
  title?: string;
  children?: React.ReactNode;
  className?: string;
}

/** Inline status message: loading failures, warnings and explanations. */
export function Notice({ tone = "info", title, children, className }: NoticeProps) {
  const { className: toneClass, Icon } = TONES[tone];
  return (
    <div
      role={tone === "error" ? "alert" : tone === "info" ? undefined : "status"}
      className={cn("flex gap-3 rounded-card border px-4 py-3 text-sm leading-6", toneClass, className)}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children}
      </div>
    </div>
  );
}
