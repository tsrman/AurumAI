"use client";

import { useRef, useState } from "react";
import { AlertCircle, CheckCircle2, Clock3, RefreshCw } from "lucide-react";
import { Button } from "@/components/primitives/Button";
import { cn } from "@/lib/utils";
import type { QuestionAnswer, QuestionResponse } from "@/types/chat";
import { useLocale, useTranslations } from "next-intl";
import { useErrorMessage } from "@/lib/errors";

export interface QuestionCardProps {
  question: QuestionResponse;
  onAnswer: (answer: QuestionAnswer, idempotencyKey: string) => Promise<void>;
}

function newIdempotencyKey(): string {
  if (typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  // randomUUID() is restricted to secure contexts, while getRandomValues()
  // remains available on HTTP deployments such as the local nip.io K8s UI.
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

function answerLabel(question: QuestionResponse, fallback: string): string {
  if (question.answer?.text) return question.answer.text;
  if (question.answer?.optionId) {
    return (
      question.options.find((option) => option.id === question.answer?.optionId)
        ?.label ?? question.answer.optionId
    );
  }
  return fallback;
}

function dateTimeLabel(value: string, locale: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

export function QuestionCard({ question, onAnswer }: QuestionCardProps) {
  const t = useTranslations("chat.question");
  const locale = useLocale();
  const errorMessage = useErrorMessage();
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const answerRef = useRef<QuestionAnswer | null>(null);
  const idempotencyKeyRef = useRef<string | null>(null);

  const submit = async (answer?: QuestionAnswer) => {
    if (submitting) return;
    const nextAnswer = answer ?? answerRef.current;
    if (!nextAnswer) return;

    answerRef.current = nextAnswer;
    setSubmitting(true);
    setError(null);
    try {
      idempotencyKeyRef.current ??= newIdempotencyKey();
      await onAnswer(nextAnswer, idempotencyKeyRef.current);
    } catch (cause) {
      setError(cause instanceof Error ? errorMessage(cause) : t("sendFailed"));
    } finally {
      setSubmitting(false);
    }
  };

  if (question.status === "answered") {
    return (
      <article className="rounded-card border border-aurum-border-soft bg-aurum-surface-2 p-4">
        <p className="text-sm font-medium text-aurum-text-2">{question.text}</p>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <span className="inline-flex items-center gap-1.5 rounded-badge bg-aurum-ok-bg px-2.5 py-1 text-sm font-medium text-aurum-ok-fg">
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
            {answerLabel(question, t("noAnswer"))}
          </span>
          <span className="text-xs text-aurum-text-3">
            {question.answeredAt
              ? t("answeredAt", { date: dateTimeLabel(question.answeredAt, locale) })
              : t("answered")}
          </span>
        </div>
      </article>
    );
  }

  if (question.status === "expired" || question.status === "cancelled") {
    const label = question.status === "expired" ? t("expired") : t("cancelled");
    return (
      <article className="rounded-card border border-aurum-border-soft bg-aurum-surface-3 p-5 text-aurum-text-2">
        <p className="text-base font-semibold">{question.text}</p>
        <p className="mt-2 inline-flex items-center gap-1.5 text-sm text-aurum-text-3">
          <Clock3 className="h-4 w-4" aria-hidden="true" />
          {label}
        </p>
      </article>
    );
  }

  const hasOptions = question.options.length > 0;

  return (
    <article className="rounded-card border border-aurum-border bg-aurum-surface p-5 shadow-card">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          {question.agentName ? (
            <p className="mb-1 text-xs font-medium text-aurum-text-3">
              {t("from", { agent: question.agentName })}
            </p>
          ) : null}
          <h3 className="text-base font-semibold text-aurum-text">{question.text}</h3>
        </div>
        <span
          className="inline-flex shrink-0 items-center gap-1.5 rounded-badge bg-aurum-accent-soft px-2.5 py-1 text-xs font-medium text-c-amber-fg"
          title={t("serverTime")}
        >
          <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
          {t("until", { date: dateTimeLabel(question.expiresAt, locale) })}
        </span>
      </div>

      {hasOptions ? (
        <div className="mt-4 flex flex-wrap gap-2" aria-label={t("options")}>
          {question.options.map((option) => (
            <Button
              key={option.id}
              variant="secondary"
              disabled={submitting}
              onClick={() => void submit({ optionId: option.id })}
              className="min-h-11"
            >
              {option.label}
            </Button>
          ))}
        </div>
      ) : (
        <div className="mt-4 flex items-end gap-3">
          <label className="min-w-0 flex-1">
            <span className="mb-1.5 block text-sm font-medium text-aurum-text-2">{t("answer")}</span>
            <input
              aria-label={t("answer")}
              value={text}
              disabled={submitting}
              onChange={(event) => {
                setText(event.target.value);
                if (error) {
                  setError(null);
                  answerRef.current = null;
                  idempotencyKeyRef.current = null;
                }
              }}
              className="min-h-11 w-full rounded-input border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text outline-none transition focus:border-aurum-accent focus:ring-2 focus:ring-aurum-accent/20 disabled:opacity-60"
            />
          </label>
          <Button
            disabled={submitting || text.trim().length === 0}
            onClick={() => void submit({ text: text.trim() })}
            className="min-h-11"
          >
            {submitting ? t("sending") : t("submit")}
          </Button>
        </div>
      )}

      {error ? (
        <div
          role="alert"
          className={cn(
            "mt-3 flex items-center gap-2 rounded-md border border-aurum-err-fg/30",
            "bg-aurum-err-bg px-3 py-2 text-sm text-aurum-err-fg"
          )}
        >
          <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1">{error}</span>
          <button
            type="button"
            disabled={submitting}
            onClick={() => void submit()}
            className="inline-flex min-h-9 items-center gap-1 rounded-btn px-2 text-xs font-semibold transition hover:bg-white/40 focus:outline-none focus:ring-2 focus:ring-aurum-accent/50 disabled:opacity-50"
          >
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            {t("retry")}
          </button>
        </div>
      ) : null}
    </article>
  );
}
