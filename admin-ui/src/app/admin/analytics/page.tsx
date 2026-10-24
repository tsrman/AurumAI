"use client";

import { useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { UsageChart, type UsageChartMetric } from "@/components/analytics/UsageChart";
import { useAdminAccess } from "@/hooks/useAdminAccess";
import { useAdminUsage, useAdminUsers } from "@/hooks/useAdminAnalytics";
import { useErrorMessage } from "@/lib/errors";
import { Button, SegmentedControl } from "@/components/primitives";
import { Notice, PageBody, PageHeader, Section, Table, Td, Th, Tr, controlClass } from "@/components/ui";
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

  if (access.isLoading) {
    return <p role="status" className="p-6 text-sm text-aurum-text-3">{accessT("checking")}</p>;
  }
  if (!access.allowed) {
    return (
      <div role="alert" className="m-6 rounded-card bg-aurum-err-bg p-6 text-sm text-aurum-err-fg">
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
    <>
    <PageHeader title={t("title")} description={t("utcNote")} />
    <PageBody>
      <form
        aria-label={t("filters")}
        onSubmit={applyFilters}
        className="grid gap-4 rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card md:grid-cols-2 lg:grid-cols-5"
      >
        <label className="text-sm">
          <span className="mb-1.5 block text-xs font-semibold text-aurum-text-3">{t("from")}</span>
          <input
            type="date"
            value={draft.from}
            onChange={(event) => updateDraft("from", event.target.value)}
            className={controlClass}
            required
          />
        </label>
        <label className="text-sm">
          <span className="mb-1.5 block text-xs font-semibold text-aurum-text-3">{t("to")}</span>
          <input
            type="date"
            value={draft.to}
            onChange={(event) => updateDraft("to", event.target.value)}
            className={controlClass}
            required
          />
        </label>
        <div className="text-sm">
          <label>
            <span className="mb-1.5 block text-xs font-semibold text-aurum-text-3">{t("user")}</span>
            <select
              value={draft.userId}
              onChange={(event) => updateDraft("userId", event.target.value)}
              className={controlClass}
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
            <span className="mt-1 block text-xs text-aurum-err-fg">
              {errorMessage(users.error)}
            </span>
          ) : null}
          {users.isLoading ? (
            <span className="mt-1 block text-xs text-aurum-text-3">
              {t("loadingUsers")}
            </span>
          ) : null}
          {/* The directory is paginated, so the dropdown only ever holds one
              page of it — these page that list, nothing on the report. */}
          <span className="mt-1 flex gap-3 text-xs text-aurum-text-2">
            <button
              type="button"
              className="hover:text-aurum-text disabled:opacity-40"
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
              className="hover:text-aurum-text disabled:opacity-40"
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
          <span className="mb-1.5 block text-xs font-semibold text-aurum-text-3">{t("model")}</span>
          <input
            type="text"
            value={draft.model}
            onChange={(event) => updateDraft("model", event.target.value)}
            className={controlClass}
            placeholder="provider/model"
          />
        </label>
        <Button type="submit" className="self-end">
          {t("apply")}
        </Button>
      </form>

      {usage.isLoading ? <p role="status" className="text-sm text-aurum-text-3">{t("loading")}</p> : null}
      {usage.error ? (
        <Notice tone="error">
          {isValidationError(usage.error) ? t("checkFilters") : errorMessage(usage.error)}
        </Notice>
      ) : null}

      {usage.data ? (
        <UsageReport
          report={usage.data.data}
          metric={metric}
          onMetricChange={setMetric}
        />
      ) : null}
    </PageBody>
    </>
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
        <div className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
          <p className="text-xs font-semibold text-aurum-text-3">{t("requestLimit")}</p>
          <p className="mt-1 text-xl font-semibold text-aurum-text">
            {report.limits.requestLimit === null
              ? t("unlimited")
              : number.format(report.limits.requestLimit)}
          </p>
          <p className="mt-1 text-xs text-aurum-text-3">{t("usedIn", { month: quotaMonth, value: number.format(report.quota.requestsUsed) })}</p>
          {requestsRemaining === null ? null : (
            <p className="text-xs text-aurum-text-2">
              {t("requestsRemaining", { value: number.format(requestsRemaining) })}
            </p>
          )}
        </div>
        <div className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
          <p className="text-xs font-semibold text-aurum-text-3">{t("tokenLimit")}</p>
          <p className="mt-1 text-xl font-semibold text-aurum-text">
            {report.limits.tokenLimit === null
              ? t("unlimited")
              : number.format(report.limits.tokenLimit)}
          </p>
          <p className="mt-1 text-xs text-aurum-text-3">{t("usedIn", { month: quotaMonth, value: number.format(report.quota.tokensUsed) })}</p>
          {tokensRemaining === null ? null : (
            <p className="text-xs text-aurum-text-2">
              {t("tokensRemaining", { value: number.format(tokensRemaining) })}
            </p>
          )}
        </div>
      </section>

      {report.totals.unaccountedRequests > 0 ? (
        <Notice tone="warning">
          {t("unaccounted", {
            count: report.totals.unaccountedRequests,
            value: number.format(report.totals.unaccountedRequests),
          })}
        </Notice>
      ) : null}
      {report.totals.requests === 0 ? (
        <Notice>{t("noData")}</Notice>
      ) : null}

      <Section
        title={t("dynamics")}
        actions={
          <SegmentedControl<UsageChartMetric>
            aria-label={t("chartMetric")}
            value={metric}
            onChange={onMetricChange}
            options={[
              { value: "requests", label: t("requests") },
              { value: "totalTokens", label: t("tokens") },
            ]}
          />
        }
      >
        <UsageChart daily={report.daily} metric={metric} />
      </Section>

      <section className="space-y-3">
        <h2 className="text-base font-semibold text-aurum-text">{t("byModel")}</h2>
        <Table>
          <thead><tr><Th>{t("model")}</Th><Th className="text-right">{t("requests")}</Th><Th className="text-right">{t("tokens")}</Th></tr></thead>
          <tbody>
            {report.byModel.map((row) => (
              <Tr key={row.model}><Td className="font-mono text-xs text-aurum-text">{row.model}</Td><Td className="text-right tabular-nums">{number.format(row.requests)}</Td><Td className="text-right tabular-nums">{number.format(row.totalTokens)}</Td></Tr>
            ))}
          </tbody>
        </Table>
      </section>

      {/* Rows come from the report, which the backend orders by tokens spent.
          Walking the paginated user directory instead put the biggest
          consumers on whichever page their UUID happened to land. */}
      <section className="space-y-3">
        <h2 className="text-base font-semibold text-aurum-text">{t("byUser")}</h2>
        {report.byUserTotal > report.byUser.length ? (
          <p className="text-sm text-aurum-text-3">
            {t("byUserTruncated", {
              shown: number.format(report.byUser.length),
              total: number.format(report.byUserTotal),
            })}
          </p>
        ) : null}
        <Table>
          <thead><tr><Th>{t("user")}</Th><Th className="text-right">{t("requests")}</Th><Th className="text-right">{t("tokens")}</Th></tr></thead>
          <tbody>
            {report.byUser.map((row) => (
              <Tr key={row.userId}><Td><span className="font-medium text-aurum-text">{row.fullName || row.email}</span><div className="text-xs text-aurum-text-3">{row.email}</div></Td><Td className="text-right tabular-nums">{number.format(row.requests)}</Td><Td className="text-right tabular-nums">{number.format(row.totalTokens)}</Td></Tr>
            ))}
          </tbody>
        </Table>
      </section>
    </>
  );
}

function SummaryCard({ label, value }: { label: string; value: number }) {
  const number = useNumberFormat();
  return (
    <div className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
      <p className="text-xs font-semibold text-aurum-text-3">{label}</p>
      <p className="mt-1 text-2xl font-semibold text-aurum-text">{number.format(value)}</p>
    </div>
  );
}
