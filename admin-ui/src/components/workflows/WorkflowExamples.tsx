"use client";

import { useState } from "react";
import { ArrowRight, CheckCircle2, FileText, GitBranch } from "lucide-react";
import { useTranslations } from "next-intl";
import {
  workflowExampleDraft,
  workflowExampleIds,
  type WorkflowExampleId,
} from "@/lib/workflow-examples";
import type { WorkflowDraft } from "@/lib/api";

export function WorkflowExamples({
  canWrite,
  onUse,
}: {
  canWrite: boolean;
  onUse: (draft: WorkflowDraft, id: WorkflowExampleId) => void;
}) {
  const t = useTranslations("workflows.examples");
  const [selected, setSelected] = useState<WorkflowExampleId | null>(null);
  return (
    <section className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">{t("title")}</h2>
        <p className="mt-2 text-sm text-aurum-text-2">{t("intro")}</p>
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        {workflowExampleIds.map((id, index) => (
          <article
            key={id}
            className={`rounded-card border bg-aurum-surface p-5 ${selected === id ? "border-aurum-accent" : "border-aurum-border"}`}
          >
            <div className="mb-5 flex items-center justify-between text-aurum-text-3">
              <FileText size={22} />
              <span className="font-mono text-xs">0{index + 1}</span>
            </div>
            <h3 className="font-semibold">{t(`${id}.title`)}</h3>
            <p className="mt-2 min-h-16 text-sm text-aurum-text-2">
              {t(`${id}.description`)}
            </p>
            <p className="my-4 flex items-center gap-2 text-xs text-aurum-text-3">
              <GitBranch size={14} />
              {t(`${id}.pattern`)}
            </p>
            <button
              type="button"
              onClick={() => setSelected(id)}
              aria-label={t("preview", { name: t(`${id}.title`) })}
              className="flex items-center gap-2 rounded-btn border border-aurum-border px-3 py-2 text-sm font-medium hover:bg-aurum-surface-2"
            >
              {t("view")}
              <ArrowRight size={15} />
            </button>
          </article>
        ))}
      </div>
      {selected ? (
        <article className="space-y-6 rounded-card border border-aurum-border bg-aurum-surface p-5 sm:p-7">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="mb-1 text-xs font-medium uppercase tracking-wider text-aurum-text-3">
                {t("walkthrough")}
              </p>
              <h3 className="text-xl font-semibold">
                {t(`${selected}.title`)}
              </h3>
            </div>
            {canWrite ? (
              <button
                type="button"
                onClick={() =>
                  onUse(workflowExampleDraft(selected, t), selected)
                }
                className="rounded-btn bg-aurum-accent px-4 py-2.5 text-sm font-semibold text-aurum-accent-ink"
              >
                {t("use")}
              </button>
            ) : null}
          </div>
          <div className="grid gap-6 lg:grid-cols-2">
            <section>
              <h4 className="mb-2 font-medium">{t("input")}</h4>
              <blockquote className="whitespace-pre-wrap rounded-input border border-aurum-border bg-aurum-surface-2 p-4 text-sm leading-6">
                {t(`${selected}.prompt`)}
              </blockquote>
            </section>
            <section>
              <h4 className="mb-2 flex items-center gap-2 font-medium">
                <CheckCircle2 size={17} />
                {t("output")}
              </h4>
              <p className="text-sm leading-6 text-aurum-text-2">
                {t(`${selected}.output`)}
              </p>
              <p className="mt-4 rounded-input bg-aurum-surface-2 p-4 text-sm leading-6">
                {t("prerequisites")}
              </p>
            </section>
          </div>
          <ol className="grid gap-3 lg:grid-cols-3">
            {[0, 1, 2].map((index) => (
              <li
                key={index}
                className="rounded-input border border-aurum-border p-4"
              >
                <h4 className="mb-2 font-medium">
                  <span className="mr-2 text-aurum-text-3">{index + 1}.</span>
                  {t(`${selected}.nodes.${index}.name`)}
                </h4>
                <p className="text-sm leading-6 text-aurum-text-2">
                  {t(`${selected}.nodes.${index}.instruction`)}
                </p>
              </li>
            ))}
          </ol>
          <p className="text-sm text-aurum-text-2">
            {t(`${selected}.connections`)}
          </p>
          <p className="text-sm text-aurum-text-3">{t("previewOnly")}</p>
        </article>
      ) : null}
    </section>
  );
}
