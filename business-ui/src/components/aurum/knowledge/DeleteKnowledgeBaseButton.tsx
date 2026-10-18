"use client";

import * as React from "react";
import { Button } from "@/components/primitives/Button";
import { ConfirmDialog } from "@/components/primitives/ConfirmDialog";
import { useKnowledge } from "@/hooks/useKnowledge";
import { useErrorMessage } from "@/lib/errors";
import { useTranslations } from "next-intl";

export interface DeleteKnowledgeBaseButtonProps {
  knowledgeBaseId: string;
  knowledgeBaseName: string;
  disabled: boolean;
  onDeleted: () => void;
}

export function DeleteKnowledgeBaseButton({
  knowledgeBaseId,
  knowledgeBaseName,
  disabled,
  onDeleted,
}: DeleteKnowledgeBaseButtonProps) {
  const t = useTranslations("knowledge.delete");
  const errorMessage = useErrorMessage();
  const { deleteBase, isDeleting } = useKnowledge();
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const busy = pending || isDeleting;

  const confirmDelete = async () => {
    if (busy) return;
    setPending(true);
    setError(null);
    try {
      await deleteBase(knowledgeBaseId);
      setConfirmOpen(false);
      onDeleted();
    } catch (deleteError) {
      setError(errorMessage(deleteError));
    } finally {
      setPending(false);
    }
  };

  return (
    <>
      <Button
        variant="danger"
        disabled={disabled || busy}
        onClick={() => {
          setError(null);
          setConfirmOpen(true);
        }}
      >
        {t("button")}
      </Button>
      {error ? (
        <p role="alert" className="mt-2 text-xs text-aurum-err-fg">
          {error}
        </p>
      ) : null}
      <ConfirmDialog
        open={confirmOpen}
        title={t("title", { name: knowledgeBaseName })}
        description={t("description")}
        confirmLabel={busy ? t("deleting") : t("confirm")}
        destructive
        confirmDisabled={busy}
        onCancel={() => {
          if (!busy) setConfirmOpen(false);
        }}
        onConfirm={() => void confirmDelete()}
      />
    </>
  );
}
