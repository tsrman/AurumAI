"use client";

import { useEffect, useRef } from "react";
import { UserBubble } from "./UserBubble";
import { AssistantBlock } from "./AssistantBlock";
import { QuestionCard } from "./QuestionCard";
import type { Message } from "@/types/aurum";
import type { QuestionAnswer, QuestionResponse } from "@/types/chat";
import { LocalizedError } from "@/lib/errors";

export interface MessageListProps {
  messages: Message[];
  questions?: QuestionResponse[];
  onRegenerate?: (messageId: string) => void;
  onAnswer?: (
    questionId: string,
    answer: QuestionAnswer,
    idempotencyKey: string
  ) => Promise<void>;
}

type TimelineItem =
  | { kind: "message"; id: string; createdAt?: string; order: number; value: Message }
  | { kind: "question"; id: string; createdAt: string; order: number; value: QuestionResponse };

function timelineItems(
  messages: Message[],
  questions: QuestionResponse[]
): TimelineItem[] {
  return [
    ...messages.map((message, order) => ({
      kind: "message" as const,
      id: message.id,
      createdAt: message.createdAt,
      order,
      value: message,
    })),
    ...questions.map((question, index) => ({
      kind: "question" as const,
      id: question.questionId,
      createdAt: question.createdAt,
      order: messages.length + index,
      value: question,
    })),
  ].sort((left, right) => {
    if (left.createdAt && right.createdAt) {
      const timestamp =
        new Date(left.createdAt).getTime() - new Date(right.createdAt).getTime();
      if (timestamp !== 0) return timestamp;
      return left.id.localeCompare(right.id);
    }
    return left.order - right.order;
  });
}

export function MessageList({
  messages,
  questions = [],
  onRegenerate,
  onAnswer,
}: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const items = timelineItems(messages, questions);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, questions.length]);

  return (
    <div
      className="flex-1 overflow-y-auto px-6 py-6"
      aria-live="polite"
      aria-atomic="false"
    >
      <div className="mx-auto flex max-w-3xl flex-col gap-6">
        {items.map((item) => {
          if (item.kind === "question") {
            return (
              <QuestionCard
                key={`question-${item.id}`}
                question={item.value}
                onAnswer={(answer, idempotencyKey) => {
                  if (!onAnswer) return Promise.reject(new LocalizedError("answerUnavailable"));
                  return onAnswer(item.id, answer, idempotencyKey);
                }}
              />
            );
          }
          const message = item.value;
          return message.role === "user" ? (
            <UserBubble key={message.id} message={message} />
          ) : (
            <AssistantBlock
              key={message.id}
              message={message}
              onRegenerate={onRegenerate}
            />
          );
        })}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
