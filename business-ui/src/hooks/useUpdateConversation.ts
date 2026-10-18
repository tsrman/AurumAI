"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { updateConversation } from "@/lib/chat-api";

export function useUpdateConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      title,
      temperature,
    }: {
      id: string;
      title?: string;
      temperature?: number | null;
    }) => updateConversation(id, { title, temperature }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}
