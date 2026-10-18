"use client";

import { useQuery } from "@tanstack/react-query";

import {
  getAdminModels,
  getAdminWorkflows,
  getAgentDefinitions,
  getKnowledgeBases,
  getSkills,
} from "@/lib/api";
import type { ResourceType } from "@/lib/api";
import { useTranslations } from "next-intl";

export interface AccessResourceOption {
  type: ResourceType;
  id: string;
  label: string;
  detail?: string;
  /** Translated into `detail` by useAccessResources (`access` namespace). */
  detailKey?: "orgReadable" | "orgClosed";
}

function ordered(options: AccessResourceOption[]): AccessResourceOption[] {
  return options.sort(
    (left, right) =>
      left.label.localeCompare(right.label, "ru") ||
      left.id.localeCompare(right.id)
  );
}

export async function loadAccessResources(
  resourceType: ResourceType
): Promise<AccessResourceOption[]> {
  if (resourceType === "model") {
    const response = await getAdminModels();
    return ordered(
      response.data.models.map((model) => ({
        type: resourceType,
        id: model.id,
        label: model.id,
        detailKey: model.organizationReadable ? "orgReadable" : "orgClosed",
      }))
    );
  }
  if (resourceType === "skill") {
    const response = await getSkills();
    return ordered(
      response.data.map((skill) => ({
        type: resourceType,
        id: skill.id,
        label: skill.name,
        detail: skill.type,
      }))
    );
  }
  if (resourceType === "knowledge_base") {
    const response = await getKnowledgeBases();
    return ordered(
      response.data.map((base) => ({
        type: resourceType,
        id: base.id,
        label: base.name,
        detail: base.description ?? undefined,
      }))
    );
  }
  if (resourceType === "agent_definition") {
    const response = await getAgentDefinitions();
    return ordered(
      response.data.map((agent) => ({
        type: resourceType,
        id: agent.id,
        label: agent.name,
        detail: agent.modelId,
      }))
    );
  }
  const response = await getAdminWorkflows(true);
  return ordered(
    response.data.map((workflow) => ({
      type: resourceType,
      id: workflow.id,
      label: workflow.name,
      detail: `${workflow.status} · v${workflow.version}`,
    }))
  );
}

export function useAccessResources(
  resourceType: ResourceType,
  options?: { enabled?: boolean }
) {
  const t = useTranslations("access");
  return useQuery({
    queryKey: ["access-resources", resourceType],
    queryFn: () => loadAccessResources(resourceType),
    select: (options: AccessResourceOption[]) =>
      options.map((option) =>
        option.detailKey ? { ...option, detail: t(option.detailKey) } : option
      ),
    enabled: options?.enabled ?? true,
  });
}
