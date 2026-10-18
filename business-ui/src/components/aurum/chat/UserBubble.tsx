import type { UserMessage } from "@/types/aurum";

export interface UserBubbleProps {
  message: UserMessage;
}

export function UserBubble({ message }: UserBubbleProps) {
  return (
    <div className="flex justify-end">
      <div className="max-w-3xl rounded-composer rounded-tr-sm bg-aurum-accent px-5 py-3 text-aurum-accent-ink shadow-card">
        <p className="whitespace-pre-wrap text-base">{message.content}</p>
      </div>
    </div>
  );
}
