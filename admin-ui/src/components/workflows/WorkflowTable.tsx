import type { WorkflowListItem } from "@/lib/api";
import { useTranslations } from "next-intl";

export function WorkflowTable({
  workflows,
  canWrite = true,
  onEdit,
  onArchive,
  onGrants,
  onSaveTemplate,
}: {
  workflows: WorkflowListItem[];
  canWrite?: boolean;
  onEdit: (id: string) => void;
  onArchive: (id: string) => void;
  onGrants: (workflow: WorkflowListItem) => void;
  onSaveTemplate?: (id: string) => void;
}) {
  const t = useTranslations("workflows.table");
  const action =
    "rounded-btn border border-aurum-border px-3 py-1.5 text-xs font-medium hover:bg-aurum-surface-2";
  return (
    <div className="min-w-0 overflow-x-auto rounded-card border border-aurum-border bg-aurum-surface">
      <table className="w-full text-left text-sm">
        <thead className="bg-aurum-surface-2 text-xs text-aurum-text-2">
          <tr>
            <th className="p-4">{t("name")}</th>
            <th className="p-4">{t("version")}</th>
            <th className="p-4">{t("status")}</th>
            <th className="p-4">{t("actions")}</th>
          </tr>
        </thead>
        <tbody>
          {workflows.map((item) => (
            <tr key={item.id} className="border-t border-aurum-border">
              <td className="max-w-72 break-words p-4">
                <p className="font-medium">{item.name}</p>
                {item.description ? (
                  <p className="mt-1 text-xs text-aurum-text-2">
                    {item.description}
                  </p>
                ) : null}
              </td>
              <td className="p-4 font-mono text-xs">{item.version}</td>
              <td className="p-4">
                <span className="rounded-chip bg-aurum-surface-3 px-2 py-1 text-xs">
                  {t(item.status === "active" ? "active" : "archived")}
                </span>
              </td>
              <td className="p-4">
                <div className="flex flex-wrap gap-2">
                  {canWrite ? (
                    <>
                      <button
                        type="button"
                        className={action}
                        onClick={() => onEdit(item.id)}
                      >
                        {t("edit")}
                      </button>
                      <button
                        type="button"
                        className={action}
                        onClick={() => onGrants(item)}
                      >
                        {t("access")}
                      </button>
                      {item.status === "active" && onSaveTemplate ? (
                        <button
                          type="button"
                          className={action}
                          onClick={() => onSaveTemplate(item.id)}
                        >
                          {t("saveTemplate")}
                        </button>
                      ) : null}
                      {item.status === "active" ? (
                        <button
                          type="button"
                          className={`${action} text-aurum-err-fg`}
                          onClick={() => onArchive(item.id)}
                        >
                          {t("archive")}
                        </button>
                      ) : null}
                    </>
                  ) : null}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
