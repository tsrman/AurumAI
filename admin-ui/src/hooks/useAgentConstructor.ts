"use client";

import { useCallback, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useAppShell } from "@/hooks/useAppShell";
import { useSkills } from "@/hooks/useSkills";
import { createAgentDefinition, getKnowledgeBases } from "@/lib/api";
import type { ConstructorState, KnowledgeBase } from "@/types/aurum";
import { useTranslations } from "next-intl";
import { useErrorMessage } from "@/lib/errors";

export function useAgentConstructor() {
  const t = useTranslations("constructor");
  const errorMessage = useErrorMessage();
  const {
    constructorState: state,
    setConstructorState: setState,
    models,
  } = useAppShell();
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
      models.some((model) => model.id === state.model)
    );
  }, [state.name, state.prompt, state.model, models]);

  const save = useCallback(async (): Promise<boolean> => {
    if (!isValid) return false;

    const skillIds = Object.keys(state.tools).filter((id) => state.tools[id]);
    const knowledgeBaseIds = Object.keys(state.bases).filter(
      (id) => state.bases[id]
    );
    setIsSaving(true);
    setSaveError(null);
    try {
      await createAgentDefinition({
        name: state.name.trim(),
        description: state.description.trim(),
        modelId: state.model,
        systemPrompt: state.prompt.trim(),
        temperature: state.temperature,
        skillIds,
        knowledgeBaseIds,
      });
      router.push("/");
      return true;
    } catch (error) {
      setSaveError(error instanceof Error ? errorMessage(error) : t("saveFailed"));
      return false;
    } finally {
      setIsSaving(false);
    }
  }, [isValid, state, router, t, errorMessage]);

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
