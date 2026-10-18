import { useCallback } from "react";
import { useTranslations } from "next-intl";

/** Keys of the `errors` message namespace that client code may raise. */
export type ErrorMessageKey =
  | "network"
  | "forbidden"
  | "notFound"
  | "conflictDependents"
  | "unknown"
  | "questionClosed"
  | "knowledgeBaseNameRequired"
  | "noActiveConversation"
  | "answerUnavailable";

/** An error whose user-facing text is resolved in the active locale at display time. */
export class LocalizedError extends Error {
  constructor(
    readonly messageKey: ErrorMessageKey,
    options?: ErrorOptions,
  ) {
    super(messageKey, options);
    this.name = "LocalizedError";
  }
}

type ErrorTranslator = (key: ErrorMessageKey | "httpStatus", values?: { status: number }) => string;

export function getApiErrorMessage(error: unknown, t: ErrorTranslator): string {
  if (error instanceof LocalizedError) {
    return t(error.messageKey);
  }

  if (error instanceof TypeError) {
    return t("network");
  }

  if (typeof error === "object" && error !== null && "response" in error) {
    const response = (error as { response?: Response }).response;
    if (response) {
      if (response.status === 403 || response.status === 401) {
        return t("forbidden");
      }
      if (response.status === 404) {
        return t("notFound");
      }
      if (response.status === 409) {
        return t("conflictDependents");
      }
      return t("httpStatus", { status: response.status });
    }
  }

  if (error instanceof Error) {
    return error.message;
  }

  return t("unknown");
}

/** Returns a formatter that renders any thrown value in the active locale. */
export function useErrorMessage(): (error: unknown) => string {
  const t: ErrorTranslator = useTranslations("errors");
  return useCallback((error: unknown) => getApiErrorMessage(error, t), [t]);
}
