"use client";

import { useQuery } from "@tanstack/react-query";
import { getChatAgentDefinitions } from "@/lib/chat-api";

/** The administrator's agents a chat turn would accept now (granted, active). */
export function useChatAgentDefinitions(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ["chat-agent-definitions"],
    queryFn: getChatAgentDefinitions,
    enabled: options?.enabled ?? true,
    staleTime: 60_000,
  });
}
