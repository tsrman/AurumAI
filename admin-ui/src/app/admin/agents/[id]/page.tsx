"use client";

import { use } from "react";
import { useTranslations } from "next-intl";
import { AgentEditor } from "@/components/agents/AgentEditor";
import { AdminGate, Notice, PageBody } from "@/components/ui";
import { useAgentDefinition } from "@/hooks/useAgentDefinitions";
import { useErrorMessage } from "@/lib/errors";

export default function EditAgentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return <AdminGate>{() => <EditAgent id={id} />}</AdminGate>;
}

function EditAgent({ id }: { id: string }) {
  const t = useTranslations("agentsAdmin.editor");
  const errorMessage = useErrorMessage();
  const agent = useAgentDefinition(id);
  if (agent.isLoading) {
    return <p role="status" className="p-6 text-sm text-aurum-text-3">{t("loading")}</p>;
  }
  if (!agent.data) {
    return (
      <PageBody>
        <Notice tone="error">{agent.error ? errorMessage(agent.error) : t("notFound")}</Notice>
      </PageBody>
    );
  }
  // Keyed so a refetched definition reseeds the form only on a new id.
  return <AgentEditor key={agent.data.data.id} agent={agent.data.data} />;
}
