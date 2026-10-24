"use client";

import { useCallback, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAdminModels } from "@/hooks/useAdminModels";
import { useAppShell } from "@/hooks/useAppShell";
import { useSkills } from "@/hooks/useSkills";
import {
  type AgentDefinition,
  createAgentDefinition,
  getKnowledgeBases,
  updateAgentDefinition,
} from "@/lib/api";
import type { ConstructorState, KnowledgeBase } from "@/types/aurum";
import { useTranslations } from "next-intl";
import { useErrorMessage } from "@/lib/errors";

function stateFromAgent(agent: AgentDefinition): ConstructorState {
  return {
    name: agent.name,
    description: agent.description ?? "",
    model: agent.modelId,
    prompt: agent.systemPrompt,
    temperature: agent.temperature,
    tools: Object.fromEntries(agent.skillIds.map((id) => [id, true])),
    bases: Object.fromEntries(agent.knowledgeBaseIds.map((id) => [id, true])),
  };
}

/**
 * Form state for creating an agent (a draft kept in the app shell, so it
 * survives navigation) or, given `agent`, for editing that definition.
 */
export function useAgentConstructor(agent?: AgentDefinition) {
  const t = useTranslations("constructor");
  const errorMessage = useErrorMessage();
  const {
    constructorState: draftState,
    setConstructorState: setDraftState,
    models,
  } = useAppShell();
  const [editState, setEditState] = useState<ConstructorState | null>(() =>
    agent ? stateFromAgent(agent) : null
  );
  const rawState = agent && editState ? editState : draftState;
  const organizationDefault = useAdminModels().data?.data.defaultModel ?? null;
  // A new draft starts on a placeholder model. Offer the organization default
  // when the catalog lists it; otherwise leave the choice to the
  // administrator: a listed model is not necessarily one agents may use.
  const state = useMemo(() => {
    if (agent || models.some((model) => model.id === rawState.model)) return rawState;
    const fallback = models.some((model) => model.id === organizationDefault) ? organizationDefault : "";
    return { ...rawState, model: fallback ?? "" };
  }, [agent, models, rawState, organizationDefault]);
  const setState = useCallback(
    (update: (prev: ConstructorState) => ConstructorState) => {
      if (agent) setEditState((prev) => update(prev ?? stateFromAgent(agent)));
      else setDraftState(update);
    },
    [agent, setDraftState]
  );
  const queryClient = useQueryClient();
  const router = useRouter();
  const skillsQuery = useSkills();
  const knowledgeQuery = useQuery({
    queryKey: ["knowledge-bases"],
    queryFn: getKnowledgeBases,
  });
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const skills = skillsQuery.data?.data ?? [];
  const knowledgeBases = useMemo<KnowledgeBase[]>(
    () =>
      (knowledgeQuery.data?.data ?? []).map((base) => ({
        id: base.id,
        name: base.name,
        badge: base.name.slice(0, 2).toUpperCase(),
        badgeColor: "amber",
        description: base.description ?? "",
        docCount: 0,
        size: "—",
        updated: new Date(base.updatedAt).toLocaleDateString("ru-RU"),
        status: "indexed",
        docs: [],
      })),
    [knowledgeQuery.data]
  );

  const setField = useCallback(
    (name: keyof ConstructorState, value: ConstructorState[keyof ConstructorState]) => {
      setState((prev) => ({ ...prev, [name]: value }));
    },
    [setState]
  );

  const toggleTool = useCallback(
    (toolName: string) => {
      setState((prev) => ({
        ...prev,
        tools: { ...prev.tools, [toolName]: !prev.tools[toolName] },
      }));
    },
    [setState]
  );

  const toggleBase = useCallback(
    (baseName: string) => {
      setState((prev) => ({
        ...prev,
        bases: { ...prev.bases, [baseName]: !prev.bases[baseName] },
      }));
    },
    [setState]
  );

  const isValid = useMemo(() => {
    return (
      state.name.trim().length > 0 &&
      state.prompt.trim().length > 0 &&
      // An edited agent may keep a model the catalog no longer lists; the
      // backend re-checks access on save.
      (models.some((model) => model.id === state.model) ||
        (agent !== undefined && state.model === agent.modelId))
    );
  }, [state.name, state.prompt, state.model, models, agent]);

  const save = useCallback(async (): Promise<boolean> => {
    if (!isValid) return false;

    const skillIds = Object.keys(state.tools).filter((id) => state.tools[id]);
    const knowledgeBaseIds = Object.keys(state.bases).filter(
      (id) => state.bases[id]
    );
    setIsSaving(true);
    setSaveError(null);
    try {
      const payload = {
        name: state.name.trim(),
        description: state.description.trim(),
        modelId: state.model,
        systemPrompt: state.prompt.trim(),
        temperature: state.temperature,
        skillIds,
        knowledgeBaseIds,
      };
      if (agent) await updateAgentDefinition(agent.id, payload);
      else await createAgentDefinition(payload);
      void queryClient.invalidateQueries({ queryKey: ["agent-definitions"] });
      router.push("/admin/agents");
      return true;
    } catch (error) {
      setSaveError(error instanceof Error ? errorMessage(error) : t("saveFailed"));
      return false;
    } finally {
      setIsSaving(false);
    }
  }, [isValid, state, agent, queryClient, router, t, errorMessage]);

  return {
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
  };
}
