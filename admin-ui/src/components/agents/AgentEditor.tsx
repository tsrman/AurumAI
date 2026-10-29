"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { useTranslations } from "next-intl";
import { ConstructorForm } from "@/components/aurum/constructor/ConstructorForm";
import { AgentPreview } from "@/components/aurum/constructor/AgentPreview";
import { PageBody, PageHeader } from "@/components/ui";
import { useAgentConstructor } from "@/hooks/useAgentConstructor";
import type { AgentDefinition } from "@/lib/api";

/** Create (no `agent`) or edit page body: form on the left, live preview on the right. */
export function AgentEditor({ agent }: { agent?: AgentDefinition }) {
  const t = useTranslations("agentsAdmin.editor");
  const router = useRouter();
  const editor = useAgentConstructor(agent);

  return (
    <>
      <PageHeader
        title={agent ? t("editTitle", { agent: agent.name }) : t("createTitle")}
        description={t("subtitle")}
        actions={
          <Link
            href="/admin/agents"
            className="inline-flex items-center gap-2 rounded-btn border border-aurum-border bg-aurum-surface px-4 py-2 text-sm font-semibold text-aurum-text-2 hover:bg-aurum-surface-3"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
            {t("back")}
          </Link>
        }
      />
      <PageBody>
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
          <div className="xl:col-span-2">
            <ConstructorForm
              state={editor.state}
              setField={editor.setField}
              toggleTool={editor.toggleTool}
              toggleBase={editor.toggleBase}
              models={editor.models}
              skills={editor.skills}
              knowledgeBases={editor.knowledgeBases}
              hermesModel={editor.hermesModel}
              kindLocked={agent !== undefined}
              isValid={editor.isValid}
              save={editor.save}
              isSaving={editor.isSaving}
              saveError={editor.saveError}
              submitLabel={agent ? t("save") : undefined}
              onCancel={() => router.push("/admin/agents")}
            />
          </div>
          <div>
            <AgentPreview state={editor.state} models={editor.models} />
          </div>
        </div>
      </PageBody>
    </>
  );
}
