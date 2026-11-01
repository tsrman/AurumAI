"use client";

import React, {
  createContext,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  localizeAgents,
  models as mockModels,
  seedConversations,
} from "@/lib/mock-data";
import { isRealMode } from "@/lib/real-mode";
import { useAuth } from "@/providers/AuthProvider";
import { useChatAgentDefinitions } from "@/hooks/useChatAgentDefinitions";
import { useModels } from "@/hooks/useModels";
import { useSettings } from "@/hooks/useSettings";
import type { ChatAgentDefinition } from "@/lib/chat-api";
import type {
  Agent,
  Conversation,
  Model,
  Source,
} from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface AppShellContextValue {
  sideMini: boolean;
  toggleSide: () => void;
  agents: Agent[];
  myAgents: Agent[];
  activeAgentId: string;
  setActiveAgentId: (id: string) => void;
  addMyAgent: (agent: Agent) => void;
  conversations: Conversation[];
  setConversations: React.Dispatch<React.SetStateAction<Conversation[]>>;
  activeConversationId: string | null;
  setActiveConversationId: (id: string | null) => void;
  models: Model[];
  activeModelId: string;
  setActiveModelId: (id: string) => void;
  drawerSource: Source | null;
  setDrawerSource: (source: Source | null) => void;
}

export const AppShellContext = createContext<AppShellContextValue | undefined>(
  undefined
);

const ACTIVE_CONVERSATION_STORAGE_PREFIX = "aurum-ai.active-conversation";
const ACTIVE_MODEL_STORAGE_PREFIX = "aurum-ai.active-model";

function activeConversationStorageKey(
  organizationId: string | undefined,
  userId: string
): string {
  return `${ACTIVE_CONVERSATION_STORAGE_PREFIX}:${organizationId ?? "no-org"}:${userId}`;
}

const AGENT_BADGE_COLORS = ["amber", "green", "blue", "violet", "rose"] as const;

/** An administrator's agent as a sidebar and picker entry. */
function agentFromDefinition(definition: ChatAgentDefinition): Agent {
  const initials = definition.name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase();
  const hash = [...definition.id].reduce((sum, char) => sum + char.charCodeAt(0), 0);
  return {
    id: definition.id,
    name: definition.name,
    badge: initials || "AG",
    badgeColor: AGENT_BADGE_COLORS[hash % AGENT_BADGE_COLORS.length],
    kind: "definition",
    kindLabel: definition.modelId,
    description: definition.description ?? "",
    capabilities: [],
    suggestions: [],
    model: definition.modelId,
  };
}

export function AppShellProvider({
  children,
  initialState,
}: {
  children?: React.ReactNode;
  initialState?: Partial<{
    drawerSource: Source | null;
    activeAgentId: string;
  }>;
}) {
  const agentsT = useTranslations("agents");
  const [sideMini, setSideMini] = useState(false);
  const [activeAgentId, setActiveAgentId] = useState(
    initialState?.activeAgentId ?? "general"
  );
  const [myAgents, setMyAgents] = useState<Agent[]>([]);
  const [conversations, setConversations] =
    useState<Conversation[]>(seedConversations);
  const [activeConversationId, setActiveConversationId] = useState<
    string | null
  >(null);
  const [drawerSource, setDrawerSource] = useState<Source | null>(
    initialState?.drawerSource ?? null
  );

  const { isAuthenticated, user } = useAuth();
  // Signed in, the sidebar offers the ordinary chat and the administrator's
  // agents; the demo catalog is only for the unauthenticated mock mode.
  const backendSession = isAuthenticated && isRealMode();
  const { data: agentDefinitions } = useChatAgentDefinitions({ enabled: backendSession });
  const agents = useMemo(() => {
    const builtIn = localizeAgents(agentsT);
    if (!backendSession) return builtIn;
    return [builtIn[0], ...(agentDefinitions ?? []).map(agentFromDefinition)];
  }, [agentsT, backendSession, agentDefinitions]);
  const activeConversationStorage = useMemo(
    () =>
      isAuthenticated && user
        ? activeConversationStorageKey(user.orgId, user.userId)
        : null,
    [isAuthenticated, user]
  );
  // A choice made while sign-in is still resolving (for example "New chat")
  // must win over the restore below, which only runs once the storage key is
  // known; otherwise the previous conversation silently comes back.
  const pendingChoice = useRef<{ id: string | null } | null>(null);
  useEffect(() => {
    if (!activeConversationStorage) return;
    const choice = pendingChoice.current;
    if (choice) {
      pendingChoice.current = null;
      if (choice.id) {
        window.sessionStorage.setItem(activeConversationStorage, choice.id);
      } else {
        window.sessionStorage.removeItem(activeConversationStorage);
      }
      return;
    }
    const restore = window.setTimeout(() => {
      setActiveConversationId(
        window.sessionStorage.getItem(activeConversationStorage)
      );
    }, 0);
    return () => window.clearTimeout(restore);
  }, [activeConversationStorage]);
  const persistActiveConversationId = useCallback(
    (id: string | null) => {
      setActiveConversationId(id);
      if (!activeConversationStorage) {
        pendingChoice.current = { id };
        return;
      }
      if (id) {
        window.sessionStorage.setItem(activeConversationStorage, id);
      } else {
        window.sessionStorage.removeItem(activeConversationStorage);
      }
    },
    [activeConversationStorage]
  );
  // Opening a conversation shows the agent it last ran on; "New chat" keeps
  // whatever is chosen.
  const openAgentId = conversations.find((c) => c.id === activeConversationId)?.agentDefinitionId;
  const [restoredFor, setRestoredFor] = useState<string | null>(null);
  if (activeConversationId && openAgentId !== undefined && restoredFor !== activeConversationId) {
    setRestoredFor(activeConversationId);
    setActiveAgentId(openAgentId ?? "general");
  }
  const { data: modelsData } = useModels({ enabled: isAuthenticated });
  const { data: settingsData, isPending: settingsPending } = useSettings({
    enabled: isAuthenticated,
  });

  const models = useMemo<Model[]>(() => {
    if (!isAuthenticated) return mockModels;
    return (modelsData?.data ?? []).map((m) => ({
      id: m.id,
      name: m.id,
      shortName: m.id,
      description: "",
      vision: m.vision === true,
    }));
  }, [isAuthenticated, modelsData]);

  const modelStorageKey = isAuthenticated && user
    ? `${ACTIVE_MODEL_STORAGE_PREFIX}:${user.orgId ?? "no-org"}:${user.userId}`
    : null;
  const [modelChoice, setModelChoice] = useState<{
    identity: string | null;
    id: string | null;
  }>({ identity: null, id: null });
  useEffect(() => {
    if (!modelStorageKey) return;
    const restore = window.setTimeout(() => {
      let stored: string | null = null;
      try {
        stored = window.sessionStorage.getItem(modelStorageKey);
      } catch {
        // Selection remains usable when browser storage is unavailable.
      }
      setModelChoice((current) => current.identity === modelStorageKey
        ? current
        : { identity: modelStorageKey, id: stored });
    }, 0);
    return () => window.clearTimeout(restore);
  }, [modelStorageKey]);
  const userSelectedModelId = modelChoice.identity === modelStorageKey
    ? modelChoice.id
    : null;
  const automaticModelId = useMemo(() => {
    if (!isAuthenticated) return mockModels[0]?.id ?? "pro";
    if (modelsData === undefined || models.length === 0) return "";
    // Wait while the settings request is still in flight, so a stored
    // default isn't overtaken by models[0]. Once it has *settled* — with
    // data or on an error — fall through: leaving activeModelId at "" made
    // every send post `model: ""` and take a 403 from a full model picker.
    if (settingsPending) return "";
    return (
      [
        settingsData?.data.default_model,
        settingsData?.data.organization_default_model,
      ].find(
        (modelId): modelId is string =>
          Boolean(modelId && models.some((model) => model.id === modelId))
      ) ?? models[0].id
    );
  }, [isAuthenticated, models, modelsData, settingsData, settingsPending]);
  const modelChoicePending = modelStorageKey && modelChoice.identity !== modelStorageKey;
  const validSelection = userSelectedModelId && (
    !isAuthenticated || models.some((model) => model.id === userSelectedModelId)
  );
  const activeModelId = modelChoicePending ? "" : validSelection
    ? userSelectedModelId
    : automaticModelId;

  const setActiveModelId = useCallback((id: string) => {
    setModelChoice({ identity: modelStorageKey, id });
    if (modelStorageKey) {
      try {
        window.sessionStorage.setItem(modelStorageKey, id);
      } catch {
        // Keep the in-memory choice if session storage is disabled or full.
      }
    }
  }, [modelStorageKey]);

  // An agent that was archived or revoked falls back to the ordinary chat.
  const effectiveAgentId = [...agents, ...myAgents].some((agent) => agent.id === activeAgentId)
    ? activeAgentId
    : "general";

  const toggleSide = useCallback(() => setSideMini((p) => !p), []);

  const addMyAgent = useCallback((agent: Agent) => {
    setMyAgents((prev) => [...prev, agent]);
  }, []);

  const value = useMemo(
    () => ({
      sideMini,
      toggleSide,
      agents,
      myAgents,
      activeAgentId: effectiveAgentId,
      setActiveAgentId,
      addMyAgent,
      conversations,
      setConversations,
      activeConversationId,
      setActiveConversationId: persistActiveConversationId,
      models,
      activeModelId,
      setActiveModelId,
      drawerSource,
      setDrawerSource,
    }),
    [
      sideMini,
      agents,
      effectiveAgentId,
      myAgents,
      conversations,
      activeConversationId,
      persistActiveConversationId,
      models,
      activeModelId,
      setActiveModelId,
      drawerSource,
      addMyAgent,
      toggleSide,
    ]
  );

  return <AppShellContext.Provider value={value}>{children}</AppShellContext.Provider>;
}
