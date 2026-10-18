import { useCallback } from "react";
import { useTranslations } from "next-intl";

type ErrorValues = Record<string, string | number>;
type ErrorTranslator = (key: string, values?: ErrorValues) => string;

/**
 * An error whose user-facing text is resolved in the active locale at display
 * time. `messageKey` is relative to the `errors` namespace; `detail` is
 * appended verbatim in parentheses (for example affected workflow node keys).
 */
export class LocalizedError extends Error {
  readonly messageKey: string;
  readonly values?: ErrorValues;
  readonly detail?: string;

  constructor(
    messageKey: string,
    options?: ErrorOptions & { values?: ErrorValues; detail?: string },
  ) {
    super(messageKey, options);
    this.name = "LocalizedError";
    this.messageKey = messageKey;
    this.values = options?.values;
    this.detail = options?.detail;
  }
}

export function getApiErrorMessage(error: unknown, t: ErrorTranslator): string {
  if (error instanceof LocalizedError) {
    const text = t(error.messageKey, error.values);
    return error.detail ? `${text} (${error.detail})` : text;
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
  const t = useTranslations("errors") as unknown as ErrorTranslator;
  return useCallback((error: unknown) => getApiErrorMessage(error, t), [t]);
}
