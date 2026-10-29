"use client";

import { useState } from "react";
import { Eye } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button, Field } from "@/components/primitives";
import { Modal, Notice, RowAction, Section, Table, Td, Th, Tr } from "@/components/ui";
import {
  useApproveHermesSkillDraft,
  useHermesSkillDraft,
  usePendingHermesSkillDrafts,
  useRejectHermesSkillDraft,
} from "@/hooks/useHermesSkillDrafts";
import { useErrorMessage } from "@/lib/errors";
import type { HermesSkillDraft } from "@/lib/api";

/** Skills Hermes Agent wrote in users' sandboxes, waiting for review. Nothing
 * runs until an administrator who has read every file approves it. */
export function HermesSkillDrafts({ canWrite }: { canWrite: boolean }) {
  const t = useTranslations("skills.hermesDrafts");
  const errorMessage = useErrorMessage();
  const { data, error } = usePendingHermesSkillDrafts();
  const [reviewing, setReviewing] = useState<HermesSkillDraft | null>(null);
  const drafts = data?.data ?? [];

  if (!error && drafts.length === 0) return null;

  return (
    <Section title={t("title")} description={t("description")}>
      {error ? <Notice tone="error">{errorMessage(error)}</Notice> : null}
      {drafts.length > 0 ? (
        <Table label={t("title")} className="shadow-none">
          <thead>
            <tr>
              <Th>{t("columns.skill")}</Th>
              <Th>{t("columns.author")}</Th>
              <Th className="text-right">{t("columns.actions")}</Th>
            </tr>
          </thead>
          <tbody>
            {drafts.map((draft) => (
              <Tr key={draft.id}>
                <Td>
                  <span className="font-medium text-aurum-text">{draft.name}</span>
                  <p className="text-xs text-aurum-text-3">{draft.description}</p>
                </Td>
                <Td>
                  <span className="text-xs text-aurum-text-2">{draft.authorEmail ?? t("unknownAuthor")}</span>
                </Td>
                <Td className="text-right">
                  <RowAction
                    icon={<Eye className="h-4 w-4" aria-hidden="true" />}
                    label={t("review")}
                    ariaLabel={t("reviewAria", { skill: draft.name })}
                    onClick={() => setReviewing(draft)}
                  />
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      ) : null}
      <ReviewDialog draft={reviewing} canWrite={canWrite} onClose={() => setReviewing(null)} />
    </Section>
  );
}

function ReviewDialog({
  draft,
  canWrite,
  onClose,
}: {
  draft: HermesSkillDraft | null;
  canWrite: boolean;
  onClose: () => void;
}) {
  const t = useTranslations("skills.hermesDrafts");
  const errorMessage = useErrorMessage();
  const detail = useHermesSkillDraft(draft?.id ?? null);
  const approve = useApproveHermesSkillDraft();
  const reject = useRejectHermesSkillDraft();
  const [name, setName] = useState("");
  const files = detail.data?.data.files;
  // The version on screen: a decision on any other is refused.
  const bundleChecksum = detail.data?.data.bundleChecksum;
  const busy = approve.isPending || reject.isPending;
  const failure = approve.error ?? reject.error ?? detail.error;
  const close = () => {
    setName("");
    approve.reset();
    reject.reset();
    onClose();
  };

  return (
    <Modal
      open={draft != null}
      title={t("reviewTitle", { skill: draft?.name ?? "" })}
      description={t("reviewDescription")}
      onClose={close}
      size="xl"
      footer={
        canWrite && draft ? (
          <div className="flex justify-end gap-2">
            <Button
              variant="secondary"
              disabled={busy || !bundleChecksum}
              onClick={() => bundleChecksum && reject.mutate({ id: draft.id, bundleChecksum }, { onSuccess: close })}
            >
              {t("reject")}
            </Button>
            <Button
              disabled={busy || !bundleChecksum}
              onClick={() =>
                bundleChecksum &&
                approve.mutate({ id: draft.id, bundleChecksum, name: name.trim() || undefined }, { onSuccess: close })
              }
            >
              {t("approve")}
            </Button>
          </div>
        ) : null
      }
    >
      {failure ? <Notice tone="error" className="mb-4">{errorMessage(failure)}</Notice> : null}
      <Field
        label={t("name")}
        placeholder={draft?.name}
        value={name}
        disabled={!canWrite}
        onChange={(event) => setName(event.target.value)}
        className="mb-4"
      />
      {files ? (
        <div className="space-y-4">
          {files.map((file) => (
            <div key={file.path}>
              <h3 className="mb-1 font-mono text-xs text-aurum-text-2">{file.path}</h3>
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-md border border-aurum-border bg-aurum-surface-3 p-3 text-xs">
                {file.content}
              </pre>
            </div>
          ))}
        </div>
      ) : detail.isLoading ? (
        <p role="status" className="text-sm text-aurum-text-3">{t("loading")}</p>
      ) : null}
    </Modal>
  );
}
