"use client";

import { Bot, FileArchive, Pencil, Trash2, Wrench, Zap } from "lucide-react";
import { Badge } from "@/components/primitives/Badge";
import { IconButton } from "@/components/primitives/IconButton";
import type { BadgeColor } from "@/types/aurum";
import type { Skill } from "@/lib/api";
import { useTranslations } from "next-intl";

export interface SkillsTableProps {
  skills: Skill[];
  onEdit: (skill: Skill) => void;
  onDelete: (skill: Skill) => void;
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

export function SkillsTable({
  skills,
  onEdit,
  onDelete,
  deletingId,
  readOnly,
}: SkillsTableProps) {
  const t = useTranslations("skills.table");
  return (
    <div className="overflow-hidden rounded-card border border-aurum-border bg-aurum-surface shadow-card">
      <table className="w-full table-fixed">
        <thead className="bg-aurum-surface-2">
          <tr>
            <th
              scope="col"
              className="w-[38%] px-4 py-2.5 text-left text-xs font-semibold text-aurum-text-3"
            >
              {t("name")}
            </th>
            <th
              scope="col"
              className="w-24 px-4 py-2.5 text-left text-xs font-semibold text-aurum-text-3"
            >
              {t("type")}
            </th>
            <th
              scope="col"
              className="w-[32%] px-4 py-2.5 text-left text-xs font-semibold text-aurum-text-3"
            >
              Endpoint / Activity
            </th>
            <th
              scope="col"
              className="w-20 px-4 py-2.5 text-left text-xs font-semibold text-aurum-text-3"
            >
              {t("actions")}
            </th>
          </tr>
        </thead>
        <tbody>
          {skills.length > 0 ? (
            skills.map((skill) => {
              const { color, Icon } = TYPE_STYLE[skill.type];
              return (
                <tr key={skill.id} className="border-b border-aurum-border-soft last:border-b-0">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <Icon className="h-4 w-4 shrink-0 text-aurum-text-3" aria-hidden="true" />
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-aurum-text">{skill.name}</p>
                        {skill.description && (
                          <p className="truncate text-xs text-aurum-text-3">{skill.description}</p>
                        )}
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <Badge color={color}>{skill.type}</Badge>
                  </td>
                  <td className="truncate px-4 py-3 text-sm text-aurum-text-2">
                    {skill.mcpEndpoint ||
                      skill.workflowType ||
                      skill.activityName ||
                      skill.bundleSkillName ||
                      "—"}
                  </td>
                  <td className="px-4 py-3">
                    {readOnly ? (
                      "—"
                    ) : (
                      <div className="flex gap-2">
                        <IconButton aria-label={t("edit")} onClick={() => onEdit(skill)}>
                          <Pencil className="h-4 w-4" aria-hidden="true" />
                        </IconButton>
                        <IconButton
                          aria-label={t("delete")}
                          onClick={() => onDelete(skill)}
                          disabled={deletingId === skill.id}
                        >
                          <Trash2 className="h-4 w-4" aria-hidden="true" />
                        </IconButton>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })
          ) : (
            <tr>
              <td colSpan={4} className="px-4 py-8 text-center text-sm text-aurum-text-3">
                {t("empty")}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
