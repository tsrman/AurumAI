/** Relative history section of a timestamp; translate with `history.buckets.*`. */
export type HistoryBucket = "today" | "yesterday" | "last7" | "last30" | "earlier";

export function historyBucket(
  input?: string | Date | null
): HistoryBucket | null {
  if (!input) return null;

  const date = typeof input === "string" ? new Date(input) : input;
  if (Number.isNaN(date.getTime())) return null;

  const startOfDayUTC = (d: Date) =>
    Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());

  const now = new Date();
  const diffDays = Math.floor(
    (startOfDayUTC(now) - startOfDayUTC(date)) / (1000 * 60 * 60 * 24)
  );

  if (diffDays <= 0) return "today";
  if (diffDays === 1) return "yesterday";
  if (diffDays < 7) return "last7";
  if (diffDays < 30) return "last30";
  return "earlier";
}

export function formatHistoryTime(
  input: string | Date | null | undefined,
  locale: string
): string {
  if (!input) return "";

  const date = typeof input === "string" ? new Date(input) : input;
  if (Number.isNaN(date.getTime())) return "";

  return date.toLocaleTimeString(locale, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Short numeric calendar date (e.g. "11.07") for display next to the time
 * on an individual history item — distinct from `historyBucket`, which
 * returns a relative section ("today", "last7") used only to group items
 * under a section header. */
export function formatShortDate(input: string | Date | null | undefined, locale: string): string {
  if (!input) return "";

  const date = typeof input === "string" ? new Date(input) : input;
  if (Number.isNaN(date.getTime())) return "";

  return date.toLocaleDateString(locale, {
    day: "2-digit",
    month: "2-digit",
  });
}
