import type { UserMessage } from "@/types/aurum";
import { ConversationFiles } from "./ConversationFiles";
import { ConversationImages } from "./ConversationImages";

export interface UserBubbleProps {
  message: UserMessage;
}

export function UserBubble({ message }: UserBubbleProps) {
  const images = message.images ?? [];
  return (
    <div className="flex flex-col items-end gap-2">
      {images.length > 0 ? (
        <div className="w-full max-w-xs">
          <ConversationImages imageIds={images} variant="attached" />
        </div>
      ) : null}
      {message.files && message.files.length > 0 ? (
        <div className="w-full max-w-md">
          <ConversationFiles files={message.files} variant="attached" />
        </div>
      ) : null}
      {message.content ? (
        <div className="max-w-3xl rounded-composer rounded-tr-sm bg-aurum-accent px-5 py-3 text-aurum-accent-ink shadow-card">
          <p className="whitespace-pre-wrap text-base">{message.content}</p>
        </div>
      ) : null}
    </div>
  );
}
