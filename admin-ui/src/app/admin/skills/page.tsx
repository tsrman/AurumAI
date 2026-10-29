"use client";

import { useMemo, useState } from "react";
import { Plus, Wrench } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button, ConfirmDialog, SegmentedControl } from "@/components/primitives";
import { AccessDialog, type AccessTarget } from "@/components/authorization/AccessDialog";
import { HermesSkillDrafts } from "@/components/aurum/admin/HermesSkillDrafts";
import { SkillFormDialog } from "@/components/aurum/admin/SkillFormDialog";
import { SkillsTable } from "@/components/aurum/admin/SkillsTable";
import { AdminGate, EmptyState, Notice, PageBody, PageHeader, SearchInput } from "@/components/ui";
import { useDeleteSkill, useSkills } from "@/hooks/useSkills";
import { useErrorMessage } from "@/lib/errors";
import type { Skill } from "@/lib/api";

type TypeFilter = "all" | Skill["type"];

export default function AdminSkillsPage() {
  return <AdminGate>{({ canWrite }) => <SkillsContent canWrite={canWrite} />}</AdminGate>;
}

function SkillsContent({ canWrite }: { canWrite: boolean }) {
  const t = useTranslations("skills.page");
  const typesT = useTranslations("skills.table.types");
  const errorMessage = useErrorMessage();
  const { data, isLoading, error } = useSkills();
  const remove = useDeleteSkill();
  const [formOpen, setFormOpen] = useState(false);
  const [editingSkill, setEditingSkill] = useState<Skill | null>(null);
  const [deletingSkill, setDeletingSkill] = useState<Skill | null>(null);
  const [access, setAccess] = useState<AccessTarget | null>(null);
  const [search, setSearch] = useState("");
  const [type, setType] = useState<TypeFilter>("all");
  const skills = useMemo(() => data?.data ?? [], [data]);
  const filtered = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return skills.filter(
      (skill) =>
        (type === "all" || skill.type === type) &&
        (!query || `${skill.name}\n${skill.description ?? ""}`.toLocaleLowerCase().includes(query))
    );
  }, [skills, search, type]);

  const openCreate = () => {
    setEditingSkill(null);
    setFormOpen(true);
  };

  return (
    <>
      <PageHeader
        title={t("title")}
        description={t("subtitle")}
        actions={
          canWrite ? (
            <Button onClick={openCreate}>
              <Plus className="h-4 w-4" aria-hidden="true" />
              {t("addSkill")}
            </Button>
          ) : null
        }
      />
      <PageBody>
        <HermesSkillDrafts canWrite={canWrite} />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <SearchInput value={search} onChange={setSearch} placeholder={t("search")} />
          <SegmentedControl<TypeFilter>
            aria-label={t("typeFilter")}
            value={type}
            onChange={setType}
            options={[
              { value: "all", label: t("allTypes") },
              { value: "mcp", label: typesT("mcp") },
              { value: "code", label: typesT("code") },
              { value: "tool", label: typesT("tool") },
              { value: "agent", label: typesT("agent") },
            ]}
          />
        </div>

        {error && !isLoading ? <Notice tone="error">{errorMessage(error)}</Notice> : null}

        {isLoading ? (
          <p role="status" className="text-sm text-aurum-text-3">{t("loading")}</p>
        ) : filtered.length === 0 && !error ? (
          <EmptyState
            icon={<Wrench className="h-8 w-8" aria-hidden="true" />}
            title={skills.length ? t("noMatches") : t("empty")}
            description={skills.length ? undefined : t("emptyHint")}
            action={
              canWrite && !skills.length ? (
                <Button onClick={openCreate}>
                  <Plus className="h-4 w-4" aria-hidden="true" />
                  {t("addSkill")}
                </Button>
              ) : undefined
            }
          />
        ) : filtered.length > 0 ? (
          <SkillsTable
            skills={filtered}
            onEdit={(skill) => {
              setEditingSkill(skill);
              setFormOpen(true);
            }}
            onDelete={setDeletingSkill}
            onAccess={(skill) => setAccess({ resourceType: "skill", resourceId: skill.id, label: skill.name })}
            deletingId={remove.isPending ? deletingSkill?.id : null}
            readOnly={!canWrite}
          />
        ) : null}
      </PageBody>

      <SkillFormDialog open={formOpen} skill={editingSkill} onClose={() => setFormOpen(false)} />
      <AccessDialog target={access} onClose={() => setAccess(null)} />
      <ConfirmDialog
        open={deletingSkill != null}
        title={t("deleteTitle")}
        description={
          remove.isError
            ? errorMessage(remove.error)
            : deletingSkill
              ? t("deleteDescription", { skill: deletingSkill.name })
              : undefined
        }
        confirmLabel={t("delete")}
        destructive
        onConfirm={() => {
          if (deletingSkill) remove.mutate(deletingSkill.id, { onSuccess: () => setDeletingSkill(null) });
        }}
        onCancel={() => setDeletingSkill(null)}
      />
    </>
  );
}
