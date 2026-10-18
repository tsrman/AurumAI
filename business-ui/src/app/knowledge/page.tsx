"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { CreateKnowledgeBaseDialog } from "@/components/aurum/knowledge/CreateKnowledgeBaseDialog";
import { KnowledgeGrid } from "@/components/aurum/knowledge/KnowledgeGrid";
import { Button } from "@/components/primitives/Button";
import { useKnowledge } from "@/hooks/useKnowledge";
import { useErrorMessage } from "@/lib/errors";
import { useAuth } from "@/providers/AuthProvider";
import { useTranslations } from "next-intl";

export default function KnowledgePage() {
  const t = useTranslations("knowledge.page");
  const errorMessage = useErrorMessage();
  const router = useRouter();
  const { authorizationReady, hasCapability } = useAuth();
  const { knowledgeBases, createBase, isCreating } = useKnowledge();
  const [createOpen, setCreateOpen] = React.useState(false);
  const [createError, setCreateError] = React.useState<string | null>(null);
  const canCreate =
    authorizationReady && hasCapability("workspace.knowledge.create");

  const handleCreate = async (name: string, description: string) => {
    setCreateError(null);
    try {
      const created = await createBase(name, description);
      setCreateOpen(false);
      router.push(`/knowledge/${created.id}`);
    } catch (error) {
      setCreateError(errorMessage(error));
    }
  };

  return (
    <div className="flex h-full flex-1 flex-col">
      <header className="flex items-center justify-between gap-4 border-b border-aurum-border-soft bg-aurum-surface px-6 py-4">
        <div>
          <h1 className="text-base font-semibold text-aurum-text">{t("title")}</h1>
          <p className="text-sm text-aurum-text-3">
            {t("subtitle")}
          </p>
        </div>
        {canCreate ? (
          <Button onClick={() => setCreateOpen(true)}>{t("create")}</Button>
        ) : null}
      </header>

      <main className="flex-1 overflow-y-auto px-6 py-6">
        <KnowledgeGrid knowledgeBases={knowledgeBases} />
      </main>
      {createOpen ? (
        <CreateKnowledgeBaseDialog
          open
          pending={isCreating}
          error={createError}
          onCancel={() => {
            setCreateOpen(false);
            setCreateError(null);
          }}
          onCreate={handleCreate}
        />
      ) : null}
    </div>
  );
}
