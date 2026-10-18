"use client";

import { useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { UsageChart, type UsageChartMetric } from "@/components/analytics/UsageChart";
import { useAdminAccess } from "@/hooks/useAdminAccess";
import { useAdminUsage, useAdminUsers } from "@/hooks/useAdminAnalytics";
import { useErrorMessage } from "@/lib/errors";
import { useLocale, useTranslations } from "next-intl";


const PAGE_SIZE = 50;
/** Locale-aware integer formatting for analytics figures. */
function useNumberFormat(): Intl.NumberFormat {
  const locale = useLocale();
  return useMemo(() => new Intl.NumberFormat(locale), [locale]);
}

type AnalyticsDraft = {
  from: string;
  to: string;
  userId: string;
  model: string;
};

function isValidationError(error: unknown): boolean {
  if (typeof error === "object" && error !== null && "response" in error) {
    return (error as { response?: Response }).response?.status === 422;
  }
  return false;
}

function currentUtcPeriod(): { from: string; to: string } {
  const now = new Date();
  const year = now.getUTCFullYear();
  const month = String(now.getUTCMonth() + 1).padStart(2, "0");
  const day = String(now.getUTCDate()).padStart(2, "0");
  return { from: `${year}-${month}-01`, to: `${year}-${month}-${day}` };
}

export default function AdminAnalyticsPage() {
  const t = useTranslations("analytics");
  const accessT = useTranslations("access");
  const errorMessage = useErrorMessage();
  const router = useRouter();
  const searchParams = useSearchParams();
  const fallback = currentUtcPeriod();
  const applied = {
    from: searchParams.get("from") || fallback.from,
    to: searchParams.get("to") || fallback.to,
    userId: searchParams.get("userId") || undefined,
    model: searchParams.get("model") || undefined,
  };
  const appliedDraft: AnalyticsDraft = {
    from: applied.from,
    to: applied.to,
    userId: applied.userId ?? "",
    model: applied.model ?? "",
  };
  const appliedKey = [
    applied.from,
    applied.to,
    applied.userId ?? "",
    applied.model ?? "",
  ].join("\u0000");
  const [draftState, setDraftState] = useState({
    baseline: appliedKey,
    value: appliedDraft,
  });
  const draft = draftState.baseline === appliedKey ? draftState.value : appliedDraft;
  const [offsetState, setOffsetState] = useState({ baseline: appliedKey, value: 0 });
  const userOffset = offsetState.baseline === appliedKey ? offsetState.value : 0;
  const [metric, setMetric] = useState<UsageChartMetric>("totalTokens");
  const access = useAdminAccess();
  const usage = useAdminUsage(applied);
  const users = useAdminUsers({ limit: PAGE_SIZE, offset: userOffset });

  if (access.isLoading) return <div className="p-6">{accessT("checking")}</div>;
  if (!access.allowed) {
    return (
      <div className="rounded-md border border-red-200 bg-red-50 p-6 text-red-700">
        {access.error || accessT("adminRequired")}
      </div>
    );
  }

  const updateDraft = (field: keyof typeof draft, value: string) => {
    setDraftState({
      baseline: appliedKey,
      value: { ...draft, [field]: value },
    });
    setOffsetState({ baseline: appliedKey, value: 0 });
  };
  const applyFilters = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const next = new URLSearchParams({ from: draft.from, to: draft.to });
    if (draft.userId) next.set("userId", draft.userId);
    if (draft.model) next.set("model", draft.model);
    router.replace(`/admin/analytics?${next.toString()}`);
  };

  const userRows = users.data?.data ?? [];
  const selectedUserIsMissing =
    draft.userId !== "" && !userRows.some((row) => row.id === draft.userId);

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">{t("title")}</h1>
        <p className="mt-2 text-sm text-gray-600">
          {t("utcNote")}
        </p>
      </div>

      <form
        aria-label={t("filters")}
        onSubmit={applyFilters}
        className="grid gap-4 rounded-md border p-4 md:grid-cols-2 lg:grid-cols-5"
      >
        <label className="text-sm">
          <span className="mb-1 block font-medium">{t("from")}</span>
          <input
            type="date"
            value={draft.from}
            onChange={(event) => updateDraft("from", event.target.value)}
            className="w-full rounded-md border px-3 py-2"
            required
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block font-medium">{t("to")}</span>
          <input
            type="date"
            value={draft.to}
            onChange={(event) => updateDraft("to", event.target.value)}
            className="w-full rounded-md border px-3 py-2"
            required
          />
        </label>
        <div className="text-sm">
          <label>
            <span className="mb-1 block font-medium">{t("user")}</span>
            <select
              value={draft.userId}
              onChange={(event) => updateDraft("userId", event.target.value)}
              className="w-full rounded-md border px-3 py-2"
            >
              <option value="">{t("allUsers")}</option>
              {selectedUserIsMissing ? (
                <option value={draft.userId}>{draft.userId}</option>
              ) : null}
              {userRows.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.fullName || row.email}
                </option>
              ))}
            </select>
          </label>
          {users.error ? (
            <span className="mt-1 block text-xs text-red-600">
              {errorMessage(users.error)}
            </span>
          ) : null}
          {users.isLoading ? (
            <span className="mt-1 block text-xs text-gray-500">
              {t("loadingUsers")}
            </span>
          ) : null}
          {/* The directory is paginated, so the dropdown only ever holds one
              page of it — these page that list, nothing on the report. */}
          <span className="mt-1 flex gap-2 text-xs">
            <button
              type="button"
              aria-label={t("previousPage")}
              disabled={userOffset === 0}
              onClick={() =>
                setOffsetState({
                  baseline: appliedKey,
                  value: Math.max(0, userOffset - PAGE_SIZE),
                })
              }
            >
              {t("back")}
            </button>
            <button
              type="button"
              aria-label={t("nextPage")}
              disabled={!(users.data?.pagination?.hasMore ?? false)}
              onClick={() =>
                setOffsetState({ baseline: appliedKey, value: userOffset + PAGE_SIZE })
              }
            >
              {t("forward")}
            </button>
          </span>
        </div>
        <label className="text-sm">
          <span className="mb-1 block font-medium">{t("model")}</span>
          <input
            type="text"
            value={draft.model}
            onChange={(event) => updateDraft("model", event.target.value)}
            className="w-full rounded-md border px-3 py-2"
            placeholder="provider/model"
          />
        </label>
        <button
          type="submit"
          className="self-end rounded-md bg-primary-600 px-4 py-2 text-white"
        >
          {t("apply")}
        </button>
      </form>

      {usage.isLoading ? <div>{t("loading")}</div> : null}
      {usage.error ? (
        <div className="rounded-md border border-red-200 bg-red-50 p-4 text-red-700">
          {isValidationError(usage.error) ? t("checkFilters") : errorMessage(usage.error)}
        </div>
      ) : null}

      {usage.data ? (
        <UsageReport
          report={usage.data.data}
          metric={metric}
          onMetricChange={setMetric}
        />
      ) : null}
    </div>
  );
}

interface UsageReportProps {
  report: import("@/lib/api").AdminUsageReport;
  metric: UsageChartMetric;
  onMetricChange: (metric: UsageChartMetric) => void;
}

function UsageReport({ report, metric, onMetricChange }: UsageReportProps) {
  const t = useTranslations("analytics");
  const locale = useLocale();
  const number = useNumberFormat();
  // Remaining quota comes from the backend's monthly counter, never from
  // this report's totals: the limits are per calendar month, so a narrow
  // period would otherwise read as an almost untouched quota.
  const { requestsRemaining, tokensRemaining } = report.quota;
  const quotaMonth = new Intl.DateTimeFormat(locale, {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${report.quota.periodStart}T00:00:00Z`));

  return (
    <>
      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4" aria-label={t("totals")}>
        <SummaryCard label={t("requests")} value={report.totals.requests} />
        <SummaryCard label={t("tokens")} value={report.totals.totalTokens} />
        <div className="rounded-md border p-4">
          <p className="text-sm text-gray-600">{t("requestLimit")}</p>
          <p className="text-xl font-semibold">
            {report.limits.requestLimit === null
              ? t("unlimited")
              : number.format(report.limits.requestLimit)}
          </p>
          <p className="text-sm text-gray-600">{t("usedIn", { month: quotaMonth, value: number.format(report.quota.requestsUsed) })}</p>
          {requestsRemaining === null ? null : (
            <p className="text-sm">
              {t("requestsRemaining", { value: number.format(requestsRemaining) })}
            </p>
          )}
        </div>
        <div className="rounded-md border p-4">
          <p className="text-sm text-gray-600">{t("tokenLimit")}</p>
          <p className="text-xl font-semibold">
            {report.limits.tokenLimit === null
              ? t("unlimited")
              : number.format(report.limits.tokenLimit)}
          </p>
          <p className="text-sm text-gray-600">{t("usedIn", { month: quotaMonth, value: number.format(report.quota.tokensUsed) })}</p>
          {tokensRemaining === null ? null : (
            <p className="text-sm">
              {t("tokensRemaining", { value: number.format(tokensRemaining) })}
            </p>
          )}
        </div>
      </section>

      {report.totals.unaccountedRequests > 0 ? (
        <div className="rounded-md border border-yellow-200 bg-yellow-50 p-4 text-yellow-800">
          {t("unaccounted", {
            count: report.totals.unaccountedRequests,
            value: number.format(report.totals.unaccountedRequests),
          })}
        </div>
      ) : null}
      {report.totals.requests === 0 ? (
        <p className="rounded-md border p-4 text-gray-600">{t("noData")}</p>
      ) : null}

      <section className="space-y-4">
        <div className="flex items-center justify-between gap-4">
          <h2 className="text-xl font-semibold">{t("dynamics")}</h2>
          <div className="flex gap-2" aria-label={t("chartMetric")}>
            <button type="button" onClick={() => onMetricChange("requests")}>
              {t("requests")}
            </button>
            <button type="button" onClick={() => onMetricChange("totalTokens")}>
              {t("tokens")}
            </button>
          </div>
        </div>
        <UsageChart daily={report.daily} metric={metric} />
      </section>

      <section className="space-y-3">
        <h2 className="text-xl font-semibold">{t("byModel")}</h2>
        <table className="w-full border text-left text-sm">
          <thead><tr><th className="border px-3 py-2">{t("model")}</th><th className="border px-3 py-2">{t("requests")}</th><th className="border px-3 py-2">{t("tokens")}</th></tr></thead>
          <tbody>
            {report.byModel.map((row) => (
              <tr key={row.model}><td className="border px-3 py-2">{row.model}</td><td className="border px-3 py-2">{number.format(row.requests)}</td><td className="border px-3 py-2">{number.format(row.totalTokens)}</td></tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* Rows come from the report, which the backend orders by tokens spent.
          Walking the paginated user directory instead put the biggest
          consumers on whichever page their UUID happened to land. */}
      <section className="space-y-3">
        <h2 className="text-xl font-semibold">{t("byUser")}</h2>
        {report.byUserTotal > report.byUser.length ? (
          <p className="text-sm text-gray-600">
            {t("byUserTruncated", {
              shown: number.format(report.byUser.length),
              total: number.format(report.byUserTotal),
            })}
          </p>
        ) : null}
        <table className="w-full border text-left text-sm">
          <thead><tr><th className="border px-3 py-2">{t("user")}</th><th className="border px-3 py-2">{t("requests")}</th><th className="border px-3 py-2">{t("tokens")}</th></tr></thead>
          <tbody>
            {report.byUser.map((row) => (
              <tr key={row.userId}><td className="border px-3 py-2">{row.fullName || row.email}<div className="text-xs text-gray-500">{row.email}</div></td><td className="border px-3 py-2">{number.format(row.requests)}</td><td className="border px-3 py-2">{number.format(row.totalTokens)}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}

function SummaryCard({ label, value }: { label: string; value: number }) {
  const number = useNumberFormat();
  return <div className="rounded-md border p-4"><p className="text-sm text-gray-600">{label}</p><p className="text-2xl font-semibold">{number.format(value)}</p></div>;
}
