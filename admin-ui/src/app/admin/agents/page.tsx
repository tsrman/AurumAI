"use client";

import { useState } from "react";
import { Plus } from "lucide-react";
import { useAdminAccess } from "@/hooks/useAdminAccess";
import { useSkills, useDeleteSkill } from "@/hooks/useSkills";
import { useErrorMessage } from "@/lib/errors";
import { Button, ConfirmDialog } from "@/components/primitives";
import { SkillFormDialog } from "@/components/aurum/admin/SkillFormDialog";
import { SkillsTable } from "@/components/aurum/admin/SkillsTable";
import type { Skill } from "@/lib/api";
import { useTranslations } from "next-intl";

export default function AdminAgentsPage() {
  const t = useTranslations("agents.page");
  const accessT = useTranslations("access");
  const accessPageT = useTranslations("access.page");
  const errorMessage = useErrorMessage();
  const { allowed, canWrite, error: accessError, isLoading: checking } = useAdminAccess();
  const { data, isLoading, error } = useSkills();
  const remove = useDeleteSkill();

  const [formOpen, setFormOpen] = useState(false);
  const [editingSkill, setEditingSkill] = useState<Skill | null>(null);
  const [deletingSkill, setDeletingSkill] = useState<Skill | null>(null);

  if (checking) {
    return <div className="p-6 text-sm text-aurum-text-3">{accessT("checking")}</div>;
  }

  if (!allowed) {
    return (
      <div className="m-6 rounded-card border border-aurum-err-fg/30 bg-aurum-err-bg p-6 text-sm text-aurum-err-fg">
        {accessError || accessPageT("adminWriteRequired")}
      </div>
    );
  }

  const skills = data?.data ?? [];

  const openCreate = () => {
    setEditingSkill(null);
    setFormOpen(true);
  };

  const openEdit = (skill: Skill) => {
    setEditingSkill(skill);
    setFormOpen(true);
  };

  const confirmDelete = () => {
    if (!deletingSkill) return;
    remove.mutate(deletingSkill.id, { onSuccess: () => setDeletingSkill(null) });
  };

  return (
    <div className="flex h-full flex-1 flex-col">
      <header className="flex items-center justify-between border-b border-aurum-border-soft bg-aurum-surface px-6 py-4">
        <div>
          <h1 className="text-base font-semibold text-aurum-text">{t("title")}</h1>
          <p className="text-sm text-aurum-text-3">
            {t("subtitle")}
          </p>
        </div>
        {canWrite && (
          <Button onClick={openCreate}>
            <Plus className="h-4 w-4" aria-hidden="true" />
            {t("addSkill")}
          </Button>
        )}
      </header>

      <main className="flex-1 overflow-y-auto px-6 py-6">
        {error && !isLoading && (
          <div className="mb-6 rounded-card border border-aurum-err-fg/30 bg-aurum-err-bg p-4 text-sm text-aurum-err-fg">
            {errorMessage(error)}
          </div>
        )}

        {isLoading ? (
          <div className="text-sm text-aurum-text-3">{t("loading")}</div>
        ) : (
          <SkillsTable
            skills={skills}
            onEdit={openEdit}
            onDelete={setDeletingSkill}
            deletingId={remove.isPending ? deletingSkill?.id : null}
            readOnly={!canWrite}
          />
        )}
      </main>

      <SkillFormDialog open={formOpen} skill={editingSkill} onClose={() => setFormOpen(false)} />

      <ConfirmDialog
        open={deletingSkill != null}
        title={t("deleteTitle")}
        description={
          deletingSkill ? t("deleteDescription", { skill: deletingSkill.name }) : undefined
        }
        confirmLabel={t("delete")}
        destructive
        onConfirm={confirmDelete}
        onCancel={() => setDeletingSkill(null)}
      />
    </div>
  );
}
