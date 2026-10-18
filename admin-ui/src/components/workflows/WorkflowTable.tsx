import type { WorkflowListItem } from "@/lib/api";
import { useTranslations } from "next-intl";

export function WorkflowTable({
  workflows,
  onEdit,
  onArchive,
  onGrants,
  onSaveTemplate,
}: {
  workflows: WorkflowListItem[];
  onEdit: (id: string) => void;
  onArchive: (id: string) => void;
  onGrants: (workflow: WorkflowListItem) => void;
  onSaveTemplate?: (id: string) => void;
}) {
  const t = useTranslations("workflows.table");
  return (
    <table className="w-full border text-left text-sm">
      <thead><tr><th className="border p-2">{t("name")}</th><th className="border p-2">{t("version")}</th><th className="border p-2">{t("status")}</th><th className="border p-2">{t("actions")}</th></tr></thead>
      <tbody>
        {workflows.map((item) => (
          <tr key={item.id}>
            <td className="border p-2">{item.name}</td>
            <td className="border p-2">{item.version}</td>
            <td className="border p-2">{item.status}</td>
            <td className="border p-2 space-x-2">
              <button type="button" onClick={() => onEdit(item.id)}>{t("edit")}</button>
              <button type="button" onClick={() => onGrants(item)}>{t("access")}</button>
              {item.status === "active" && onSaveTemplate ? (
                <button
                  type="button"
                  aria-label={t("saveTemplate")}
                  onClick={() => onSaveTemplate(item.id)}
                >
                  {t("saveTemplate")}
                </button>
              ) : null}
              {item.status === "active" ? <button type="button" onClick={() => onArchive(item.id)}>{t("archive")}</button> : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
