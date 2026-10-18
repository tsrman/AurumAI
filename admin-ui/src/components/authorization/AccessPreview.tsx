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
    <section className="mt-4 rounded-md border border-aurum-border-soft p-3">
      <h4 className="text-sm font-medium text-aurum-text">{t("title")}</h4>
      {!subject ? (
        <p className="mt-2 text-xs text-aurum-text-3">{t("chooseSubject")}</p>
      ) : !supported ? (
        <p className="mt-2 text-xs text-aurum-text-3">
          {t("organizationDirect")}
        </p>
      ) : (
        <Button
          type="button"
          variant="secondary"
          disabled={preview.isPending}
          onClick={requestPreview}
        >
          {preview.isPending ? t("checking") : t("check")}
        </Button>
      )}

      {preview.isError ? (
        <p className="mt-2 text-sm text-aurum-err-fg">{errorMessage(preview.error)}</p>
      ) : null}

      {preview.data ? (
        <div className="mt-3 space-y-3 text-sm">
          <div>
            <span className="font-medium text-aurum-text">Capabilities: </span>
            <span className="text-aurum-text-2">
              {preview.data.data.capabilities.join(", ") || "—"}
            </span>
          </div>
          <ul className="space-y-2">
            {preview.data.data.resources.map((resource) => (
              <li key={`${resource.resourceType}:${resource.resourceId}`} className="rounded bg-aurum-surface-2 p-2">
                <div className="font-medium text-aurum-text">
                  {resourceLabels.get(resource.resourceId) ?? resource.resourceId}
                </div>
                <div className="text-xs text-aurum-text-3">
                  <span>
                    {resource.canWrite ? "write" : resource.canRead ? "read" : t("noAccess")} ·{" "}
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
