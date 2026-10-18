"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { deleteConversation } from "@/lib/chat-api";

export function useDeleteConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}
