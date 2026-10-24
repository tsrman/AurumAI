import type { DailyUsage } from "@/lib/api";
import { useTranslations } from "next-intl";


export type UsageChartMetric = "requests" | "totalTokens";

interface UsageChartProps {
  daily: DailyUsage[];
  metric: UsageChartMetric;
}

const WIDTH = 600;
const HEIGHT = 200;
const PADDING = 20;


export function UsageChart({ daily, metric }: UsageChartProps) {
  const t = useTranslations("analytics.chart");
  const values = daily.map((row) => row[metric]);
  const maximum = Math.max(1, ...values);
  const horizontalRange = WIDTH - PADDING * 2;
  const verticalRange = HEIGHT - PADDING * 2;
  const denominator = Math.max(1, daily.length - 1);
  const coordinates = values.map((value, index) => ({
    x: PADDING + (index / denominator) * horizontalRange,
    y: HEIGHT - PADDING - (value / maximum) * verticalRange,
  }));
  const points = coordinates
    .map(({ x, y }) => `${x.toFixed(2)},${y.toFixed(2)}`)
    .join(" ");
  // A polyline with a single coordinate draws nothing at all, and a
  // one-day period is the default view on the 1st of every UTC month —
  // so that day's usage rendered as an empty chart.
  const singlePoint = coordinates.length === 1 ? coordinates[0] : null;
  const label =
    metric === "requests"
      ? t("requestsTitle")
      : t("tokensTitle");

  return (
    <div className="space-y-4">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={label}
        className="h-64 w-full rounded-input border border-aurum-border-soft bg-aurum-surface-2"
      >
        <line
          x1={PADDING}
          y1={HEIGHT - PADDING}
          x2={WIDTH - PADDING}
          y2={HEIGHT - PADDING}
          stroke="currentColor"
          opacity="0.25"
        />
        {singlePoint ? (
          <circle
            cx={singlePoint.x.toFixed(2)}
            cy={singlePoint.y.toFixed(2)}
            r="5"
            fill="currentColor"
            className="text-aurum-accent"
          />
        ) : (
          <polyline
            points={points}
            fill="none"
            stroke="currentColor"
            strokeWidth="3"
            className="text-aurum-accent"
          />
        )}
      </svg>
      <div className="overflow-x-auto rounded-input border border-aurum-border-soft">
        <table
          className="w-full text-left text-sm"
          aria-label={t("tableLabel")}
        >
          <thead>
            <tr>
              <th className="bg-aurum-surface-2 px-3 py-2 text-xs font-semibold text-aurum-text-3">{t("dateUtc")}</th>
              <th className="bg-aurum-surface-2 px-3 py-2 text-right text-xs font-semibold text-aurum-text-3">{t("requests")}</th>
              <th className="bg-aurum-surface-2 px-3 py-2 text-right text-xs font-semibold text-aurum-text-3">{t("tokens")}</th>
            </tr>
          </thead>
          <tbody>
            {daily.map((row) => (
              <tr key={row.date} className="border-t border-aurum-border-soft text-aurum-text-2">
                <td className="px-3 py-2 font-mono text-xs">{row.date}</td>
                <td className="px-3 py-2 text-right tabular-nums">{row.requests}</td>
                <td className="px-3 py-2 text-right tabular-nums">{row.totalTokens}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
