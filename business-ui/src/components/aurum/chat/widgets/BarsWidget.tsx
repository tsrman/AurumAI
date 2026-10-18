import type { BarsWidget as BarsWidgetType } from "@/types/aurum";

export interface BarsWidgetProps {
  widget: BarsWidgetType;
}

export function BarsWidget({ widget }: BarsWidgetProps) {
  const max = widget.max || Math.max(...widget.data.map((d) => d.value), 1);

  return (
    <div
      className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card"
      role="img"
      aria-label={widget.title}
    >
      <h4 className="mb-4 text-sm font-semibold text-aurum-text">
        {widget.title}
      </h4>
      <div className="flex flex-col gap-3">
        {widget.data.map((item, index) => {
          const pct = max > 0 ? (item.value / max) * 100 : 0;
          return (
            <div key={index} className="flex items-center gap-3">
              <span className="w-12 text-sm text-aurum-text-2">
                {item.label}
              </span>
              <div
                className="flex-1 overflow-hidden rounded-full bg-aurum-surface-3"
                aria-hidden="true"
              >
                <div
                  className="h-2 rounded-full bg-aurum-accent transition-all"
                  style={{ width: `${pct}%` }}
                />
              </div>
              <span className="w-16 text-right text-sm font-medium text-aurum-text">
                {item.value}
                {widget.unit}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
