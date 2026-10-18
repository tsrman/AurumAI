import type { TableWidget as TableWidgetType } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface TableWidgetProps {
  widget: TableWidgetType;
}

export function TableWidget({ widget }: TableWidgetProps) {
  const t = useTranslations("chat");
  return (
    <div className="overflow-hidden rounded-card border border-aurum-border bg-aurum-surface shadow-card">
      <div className="overflow-x-auto">
        <table className="w-full text-sm" aria-label={t("queryResult")}>
          <thead className="bg-aurum-surface-2">
            <tr>
              {widget.columns.map((col, index) => (
                <th
                  key={index}
                  scope="col"
                  className="px-4 py-2 text-left font-semibold text-aurum-text-2"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {widget.rows.map((row, rowIndex) => (
              <tr
                key={rowIndex}
                className="border-t border-aurum-border-soft last:border-b-0"
              >
                {row.map((cell, cellIndex) => (
                  <td
                    key={cellIndex}
                    className="px-4 py-2 text-aurum-text"
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
