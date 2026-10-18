"use client";

import { ConstructorForm } from "@/components/aurum/constructor/ConstructorForm";
import { AgentPreview } from "@/components/aurum/constructor/AgentPreview";
import { useAgentConstructor } from "@/hooks/useAgentConstructor";
import { useTranslations } from "next-intl";

export default function ConstructorPage() {
  const t = useTranslations("constructor");
  const {
    state,
    setField,
    toggleTool,
    toggleBase,
    isValid,
    save,
    models,
    skills,
    knowledgeBases,
    isSaving,
    saveError,
  } = useAgentConstructor();

  return (
    <div>
      <h1 className="mb-6 text-2xl font-semibold text-aurum-text">{t("title")}</h1>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <ConstructorForm
            state={state}
            setField={setField}
            toggleTool={toggleTool}
            toggleBase={toggleBase}
            models={models}
            skills={skills}
            knowledgeBases={knowledgeBases}
            isValid={isValid}
            save={save}
            isSaving={isSaving}
            saveError={saveError}
          />
        </div>

        <div className="lg:col-span-1">
          <AgentPreview state={state} models={models} />
        </div>
      </div>
    </div>
  );
}
