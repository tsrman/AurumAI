"use client";

import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  answerQuestion,
  getActiveTurn,
  listQuestions,
  subscribeTurnEvents,
} from "@/lib/questions-api";
import type { QuestionAnswer, QuestionResponse, TurnStreamEvent } from "@/types/chat";
import { LocalizedError } from "@/lib/errors";

export const questionKeys = {
  list: (conversationId: string) => ["questions", conversationId] as const,
  activeTurn: (conversationId: string) => ["turn", "active", conversationId] as const,
};

export function useQuestions(conversationId: string | null) {
  const queryClient = useQueryClient();
  const versionsRef = useRef(new Map<string, number>());
  const turnVersionsRef = useRef(new Map<string, number>());
  const enabled = Boolean(conversationId);
  const questionsQuery = useQuery({
    queryKey: questionKeys.list(conversationId ?? ""),
    queryFn: () => listQuestions(conversationId as string),
    enabled,
  });
  const turnQuery = useQuery({
    queryKey: questionKeys.activeTurn(conversationId ?? ""),
    queryFn: () => getActiveTurn(conversationId as string),
    enabled,
  });

  useEffect(() => {
    for (const question of questionsQuery.data ?? []) {
      versionsRef.current.set(
        question.questionId,
        Math.max(versionsRef.current.get(question.questionId) ?? 0, question.version)
      );
    }
  }, [questionsQuery.data]);

  useEffect(() => {
    const turn = turnQuery.data;
    if (!turn) return;
    turnVersionsRef.current.set(
      turn.turnId,
      Math.max(turnVersionsRef.current.get(turn.turnId) ?? 0, turn.version)
    );
  }, [turnQuery.data]);

  useEffect(() => {
    const turn = turnQuery.data;
    if (!conversationId || !turn || turn.status !== "active") return;

    const handleEvent = (event: TurnStreamEvent) => {
      if (event.data.turnId !== turn.turnId) return;
      if (event.type === "question-status") {
        const current = versionsRef.current.get(event.data.questionId) ?? 0;
        if (event.data.version <= current) return;
        versionsRef.current.set(event.data.questionId, event.data.version);
        void queryClient.invalidateQueries({
          queryKey: questionKeys.list(conversationId),
        });
        return;
      }
      const current = turnVersionsRef.current.get(event.data.turnId) ?? 0;
      if (event.data.version <= current) return;
      turnVersionsRef.current.set(event.data.turnId, event.data.version);
      void queryClient.invalidateQueries({
        queryKey: questionKeys.activeTurn(conversationId),
      });
    };

    const subscription = subscribeTurnEvents(
      conversationId,
      turn.turnId,
      handleEvent,
      () => {
        // The durable 5-second reconciliation belongs to the server stream.
        // subscribeTurnEvents reconnects transient failures with backoff and
        // receives a fresh snapshot; errors do not mutate durable state.
      }
    );
    return subscription.cancel;
  }, [conversationId, queryClient, turnQuery.data]);

  const answer = async (
    questionId: string,
    answerValue: QuestionAnswer,
    idempotencyKey: string
  ): Promise<void> => {
    if (!conversationId) throw new LocalizedError("noActiveConversation");
    const updated = await answerQuestion(
      conversationId,
      questionId,
      answerValue,
      idempotencyKey
    );
    versionsRef.current.set(updated.questionId, updated.version);
    queryClient.setQueryData<QuestionResponse[]>(
      questionKeys.list(conversationId),
      (current = []) =>
        current.some((item) => item.questionId === updated.questionId)
          ? current.map((item) =>
              item.questionId === updated.questionId ? updated : item
            )
          : [...current, updated]
    );
    await queryClient.invalidateQueries({
      queryKey: questionKeys.activeTurn(conversationId),
    });
  };

  return {
    questions: questionsQuery.data ?? [],
    activeTurn: turnQuery.data ?? null,
    hasActiveTurn: turnQuery.data?.status === "active",
    isLoading: questionsQuery.isLoading || turnQuery.isLoading,
    error: questionsQuery.error ?? turnQuery.error ?? null,
    answer,
  };
}
