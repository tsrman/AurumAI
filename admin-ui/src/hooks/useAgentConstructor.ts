"use client";

import { useCallback, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAdminModels, useOrganizationHermesSettings } from "@/hooks/useAdminModels";
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
    kind: agent.kind ?? "agent",
    name: agent.name,
    description: agent.description ?? "",
    model: agent.modelId,
    prompt: agent.systemPrompt,
    temperature: agent.temperature,
    tools: Object.fromEntries(agent.skillIds.map((id) => [id, true])),
    bases: Object.fromEntries(agent.knowledgeBaseIds.map((id) => [id, true])),
    webEnabled: agent.webEnabled ?? true,
    imagesEnabled: agent.imagesEnabled ?? true,
    askUserEnabled: agent.askUserEnabled ?? true,
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
  // A Hermes node runs on the organization's Hermes model, not its own.
  const hermesModel = useOrganizationHermesSettings().data?.data.model ?? null;
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
    if (state.kind === "hermes") {
      return state.name.trim().length > 0 && state.prompt.trim().length > 0 && Boolean(hermesModel);
    }
    return (
      state.name.trim().length > 0 &&
      state.prompt.trim().length > 0 &&
      // An edited agent may keep a model the catalog no longer lists; the
      // backend re-checks access on save.
      (models.some((model) => model.id === state.model) ||
        (agent !== undefined && state.model === agent.modelId))
    );
  }, [state.kind, state.name, state.prompt, state.model, models, agent, hermesModel]);

  const save = useCallback(async (): Promise<boolean> => {
    if (!isValid) return false;

    const hermes = state.kind === "hermes";
    // Hermes brings the platform's tools and its own skills.
    const skillIds = hermes ? [] : Object.keys(state.tools).filter((id) => state.tools[id]);
    const knowledgeBaseIds = hermes ? [] : Object.keys(state.bases).filter((id) => state.bases[id]);
    setIsSaving(true);
    setSaveError(null);
    try {
      const payload = {
        name: state.name.trim(),
        description: state.description.trim(),
        modelId: hermes ? (hermesModel ?? state.model) : state.model,
        systemPrompt: state.prompt.trim(),
        temperature: state.temperature,
        skillIds,
        knowledgeBaseIds,
        webEnabled: state.webEnabled,
        imagesEnabled: state.imagesEnabled,
        askUserEnabled: state.askUserEnabled,
      };
      if (agent) await updateAgentDefinition(agent.id, payload);
      else await createAgentDefinition({ ...payload, kind: state.kind });
      void queryClient.invalidateQueries({ queryKey: ["agent-definitions"] });
      router.push("/admin/agents");
      return true;
    } catch (error) {
      setSaveError(error instanceof Error ? errorMessage(error) : t("saveFailed"));
      return false;
    } finally {
      setIsSaving(false);
    }
  }, [isValid, state, agent, queryClient, router, t, errorMessage, hermesModel]);

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
    hermesModel,
    isSaving,
    saveError,
  };
}
