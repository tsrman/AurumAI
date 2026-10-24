"use client";

import { useTranslations } from "next-intl";
import { Modal } from "@/components/ui/Modal";
import type { ResourceType } from "@/lib/api";
import { GrantEditor } from "./GrantEditor";

export interface AccessTarget {
  resourceType: ResourceType;
  resourceId: string;
  label: string;
}

/** Per-resource access control, opened from a row's "Access" action. */
export function AccessDialog({ target, onClose }: { target: AccessTarget | null; onClose: () => void }) {
  const t = useTranslations("authorization.dialog");
  return (
    <Modal
      open={target !== null}
      title={target ? t("title", { resource: target.label }) : ""}
      description={target ? t(`description.${target.resourceType}`) : undefined}
      onClose={onClose}
    >
      {target ? (
        <GrantEditor
          key={`${target.resourceType}:${target.resourceId}`}
          resourceType={target.resourceType}
          resourceId={target.resourceId}
          resourceLabel={target.label}
        />
      ) : null}
    </Modal>
  );
}
