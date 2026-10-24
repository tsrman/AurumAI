"use client";

import { Bot, FileArchive, KeyRound, Pencil, Trash2, Wrench, Zap } from "lucide-react";
import { useTranslations } from "next-intl";
import { Badge } from "@/components/primitives/Badge";
import { RowAction, RowActions, Table, Td, Th, Tr } from "@/components/ui";
import type { BadgeColor } from "@/types/aurum";
import type { Skill } from "@/lib/api";

export interface SkillsTableProps {
  skills: Skill[];
  onEdit: (skill: Skill) => void;
  onDelete: (skill: Skill) => void;
  onAccess: (skill: Skill) => void;
  deletingId?: string | null;
  /** admin:read-only viewer — hides edit/delete controls. */
  readOnly?: boolean;
}

const TYPE_STYLE: Record<Skill["type"], { color: BadgeColor; Icon: typeof Bot }> = {
  agent: { color: "violet", Icon: Bot },
  tool: { color: "blue", Icon: Wrench },
  mcp: { color: "green", Icon: Zap },
  code: { color: "amber", Icon: FileArchive },
};

export function SkillsTable({ skills, onEdit, onDelete, onAccess, deletingId, readOnly }: SkillsTableProps) {
  const t = useTranslations("skills.table");
  return (
    <Table label={t("label")}>
      <thead>
        <tr>
          <Th>{t("name")}</Th>
          <Th>{t("type")}</Th>
          <Th>{t("target")}</Th>
          <Th className="text-right">{t("actions")}</Th>
        </tr>
      </thead>
      <tbody>
        {skills.map((skill) => {
          const { color, Icon } = TYPE_STYLE[skill.type];
          return (
            <Tr key={skill.id}>
              <Td>
                <div className="flex items-center gap-3">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn bg-aurum-surface-3 text-aurum-text-2">
                    <Icon className="h-4 w-4" aria-hidden="true" />
                  </span>
                  <div className="min-w-0">
                    <p className="truncate font-medium text-aurum-text">{skill.name}</p>
                    {skill.description && <p className="line-clamp-1 text-xs text-aurum-text-3">{skill.description}</p>}
                  </div>
                </div>
              </Td>
              <Td><Badge color={color}>{t(`types.${skill.type}`)}</Badge></Td>
              <Td className="max-w-[16rem] truncate font-mono text-xs">
                {skill.mcpEndpoint || skill.workflowType || skill.activityName || skill.bundleSkillName || "—"}
              </Td>
              <Td>
                {readOnly ? (
                  <span className="block text-right">—</span>
                ) : (
                  <RowActions>
                    <RowAction icon={<KeyRound className="h-4 w-4" aria-hidden="true" />} label={t("access")} ariaLabel={t("accessAria", { skill: skill.name })} onClick={() => onAccess(skill)} />
                    <RowAction icon={<Pencil className="h-4 w-4" aria-hidden="true" />} label={t("edit")} ariaLabel={t("editAria", { skill: skill.name })} onClick={() => onEdit(skill)} />
                    <RowAction tone="danger" icon={<Trash2 className="h-4 w-4" aria-hidden="true" />} label={t("delete")} ariaLabel={t("deleteAria", { skill: skill.name })} onClick={() => onDelete(skill)} disabled={deletingId === skill.id} />
                  </RowActions>
                )}
              </Td>
            </Tr>
          );
        })}
      </tbody>
    </Table>
  );
}
