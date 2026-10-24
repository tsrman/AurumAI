"use client";

import { Button } from "@/components/primitives/Button";
import { useAccessPreview } from "@/hooks/useAuthorization";
import { useErrorMessage } from "@/lib/errors";
import type { AuthorizationPrincipal, ResourceType } from "@/lib/api";
import { useTranslations } from "next-intl";

export type PreviewSubject = AuthorizationPrincipal;

export interface PreviewResource {
  resourceType: ResourceType;
  resourceId: string;
  label: string;
}

export interface AccessPreviewProps {
  subject: PreviewSubject | null;
  resources: PreviewResource[];
}

const SOURCES = new Set(["grant", "admin_bypass", "none"]);

export function AccessPreview({ subject, resources }: AccessPreviewProps) {
  const t = useTranslations("authorization.preview");
  const errorMessage = useErrorMessage();
  const preview = useAccessPreview();
  const resourceLabels = new Map(resources.map((item) => [item.resourceId, item.label]));
  const supported = subject?.principalType === "group" || subject?.principalType === "user";

  const requestPreview = () => {
    if (
      !subject ||
      (subject.principalType !== "group" && subject.principalType !== "user")
    ) {
      return;
    }
    preview.mutate({
      subjectType: subject.principalType,
      subjectId: subject.principalId,
      resources: resources.map(({ resourceType, resourceId }) => ({
        resourceType,
        resourceId,
      })),
    });
  };

  return (
    <section className="rounded-input border border-aurum-border-soft p-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h4 className="text-sm font-medium text-aurum-text">{t("title")}</h4>
          <p className="mt-0.5 text-xs text-aurum-text-3">
            {!subject ? t("chooseSubject") : !supported ? t("organizationDirect") : t("hint", { subject: subject.label })}
          </p>
        </div>
        {subject && supported ? (
          <Button type="button" variant="secondary" disabled={preview.isPending} onClick={requestPreview}>
            {preview.isPending ? t("checking") : t("check")}
          </Button>
        ) : null}
      </div>

      {preview.isError ? (
        <p className="mt-2 text-sm text-aurum-err-fg">{errorMessage(preview.error)}</p>
      ) : null}

      {preview.data ? (
        <div className="mt-3 space-y-3 text-sm">
          <div>
            <p className="text-xs font-semibold text-aurum-text-3">{t("capabilities")}</p>
            <div className="mt-1 flex flex-wrap gap-1">
              {preview.data.data.capabilities.length ? (
                preview.data.data.capabilities.map((capability) => (
                  <code key={capability} className="rounded bg-aurum-surface-3 px-1.5 py-0.5 text-xs text-aurum-text-2">
                    {capability}
                  </code>
                ))
              ) : (
                <span className="text-aurum-text-3">—</span>
              )}
            </div>
          </div>
          <ul className="space-y-2">
            {preview.data.data.resources.map((resource) => (
              <li key={`${resource.resourceType}:${resource.resourceId}`} className="rounded bg-aurum-surface-2 p-2">
                <div className="font-medium text-aurum-text">
                  {resourceLabels.get(resource.resourceId) ?? resource.resourceId}
                </div>
                <div className="text-xs text-aurum-text-3">
                  <span>
                    {resource.canWrite ? t("canWrite") : resource.canRead ? t("canRead") : t("noAccess")} ·{" "}
                  </span>
                  <span>{SOURCES.has(resource.source) ? t(`source.${resource.source}`) : resource.source}</span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
