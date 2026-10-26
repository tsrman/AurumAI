"use client";

import { useQuery } from "@tanstack/react-query";
import { getChatAgents } from "@/lib/chat-api";

/** Agents the backend would accept a turn for now (Hermes Agent). */
export function useChatAgents(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ["chat-agents"],
    queryFn: getChatAgents,
    enabled: options?.enabled ?? true,
    staleTime: 60_000,
  });
}
