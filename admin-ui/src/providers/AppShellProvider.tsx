"use client";

import React, { createContext, useMemo, useState } from "react";
import { models as mockModels } from "@/lib/mock-data";
import { useAuth } from "@/providers/AuthProvider";
import { useModels } from "@/hooks/useModels";
import type { Model, ConstructorState } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface AppShellContextValue {
  models: Model[];
  constructorState: ConstructorState;
  setConstructorState: React.Dispatch<React.SetStateAction<ConstructorState>>;
}

export const AppShellContext = createContext<AppShellContextValue | undefined>(
  undefined
);

const DEFAULT_CONSTRUCTOR: ConstructorState = {
  kind: "agent",
  name: "",
  description: "",
  model: mockModels[0]?.id ?? "",
  // Localized in AppShellProvider (`constructor.defaultPrompt`).
  prompt: "",
  temperature: 0.3,
  tools: {},
  bases: {},
  webEnabled: true,
  imagesEnabled: true,
  askUserEnabled: true,
};

export function AppShellProvider({ children }: { children?: React.ReactNode }) {
  const t = useTranslations("constructor");
  const [constructorState, setConstructorState] = useState<ConstructorState>(() => ({
    ...DEFAULT_CONSTRUCTOR,
    prompt: t("defaultPrompt"),
  }));

  const { isAuthenticated } = useAuth();
  const { data: modelsData } = useModels({ enabled: isAuthenticated });

  const models = useMemo<Model[]>(() => {
    const real = modelsData?.data;
    if (!real || real.length === 0) return mockModels;
    return real.map((m) => ({
      id: m.id,
      name: m.id,
      shortName: m.id,
      description: "",
    }));
  }, [modelsData]);

  const value = useMemo(
    () => ({
      models,
      constructorState,
      setConstructorState,
    }),
    [models, constructorState]
  );

  return <AppShellContext.Provider value={value}>{children}</AppShellContext.Provider>;
}
