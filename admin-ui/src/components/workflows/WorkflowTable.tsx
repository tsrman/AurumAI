import { Archive, BookmarkPlus, GitBranch, KeyRound, Pencil } from "lucide-react";
import { useTranslations } from "next-intl";
import { Badge } from "@/components/primitives/Badge";
import { RowAction, RowActions, Table, Td, Th, Tr } from "@/components/ui";
import type { WorkflowListItem } from "@/lib/api";

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
  const icon = "h-4 w-4";
  return (
    <Table label={t("label")}>
      <thead>
        <tr>
          <Th>{t("name")}</Th>
          <Th>{t("version")}</Th>
          <Th>{t("status")}</Th>
          <Th className="text-right">{t("actions")}</Th>
        </tr>
      </thead>
      <tbody>
        {workflows.map((item) => (
          <Tr key={item.id}>
            <Td className="max-w-80">
              <div className="flex items-center gap-3">
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn bg-c-violet-bg text-c-violet-fg">
                  <GitBranch className={icon} aria-hidden="true" />
                </span>
                <div className="min-w-0">
                  <p className="break-words font-medium text-aurum-text">{item.name}</p>
                  {item.description ? (
                    <p className="mt-0.5 line-clamp-2 text-xs text-aurum-text-3">{item.description}</p>
                  ) : null}
                </div>
              </div>
            </Td>
            <Td className="font-mono text-xs">{item.version}</Td>
            <Td>
              <Badge color={item.status === "active" ? "green" : "rose"}>
                {t(item.status === "active" ? "active" : "archived")}
              </Badge>
            </Td>
            <Td>
              {canWrite ? (
                <RowActions>
                  <RowAction icon={<Pencil className={icon} aria-hidden="true" />} label={t("edit")} onClick={() => onEdit(item.id)} />
                  <RowAction icon={<KeyRound className={icon} aria-hidden="true" />} label={t("access")} onClick={() => onGrants(item)} />
                  {item.status === "active" && onSaveTemplate ? (
                    <RowAction icon={<BookmarkPlus className={icon} aria-hidden="true" />} label={t("saveTemplate")} onClick={() => onSaveTemplate(item.id)} />
                  ) : null}
                  {item.status === "active" ? (
                    <RowAction tone="danger" icon={<Archive className={icon} aria-hidden="true" />} label={t("archive")} onClick={() => onArchive(item.id)} />
                  ) : null}
                </RowActions>
              ) : null}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}
