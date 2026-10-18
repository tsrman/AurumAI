import type { MetricsWidget as MetricsWidgetType } from "@/types/aurum";

export interface MetricsWidgetProps {
  widget: MetricsWidgetType;
}

export function MetricsWidget({ widget }: MetricsWidgetProps) {
  return (
    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      {widget.items.map((item, index) => (
        <div
          key={index}
          className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card"
        >
          <dt className="text-sm text-aurum-text-3">{item.label}</dt>
          <dd className="mt-1 text-2xl font-semibold text-aurum-text">
            {item.value}
          </dd>
          <dd
            className={`mt-1 text-sm font-medium ${
              item.isPositive ? "text-aurum-ok-fg" : "text-aurum-err-fg"
            }`}
          >
            {item.delta}
          </dd>
        </div>
      ))}
    </dl>
  );
}
