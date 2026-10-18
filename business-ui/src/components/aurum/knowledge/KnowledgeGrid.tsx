import { KnowledgeBaseCard } from "./KnowledgeBaseCard";
import type { KnowledgeBase } from "@/types/aurum";

export interface KnowledgeGridProps {
  knowledgeBases: KnowledgeBase[];
}

export function KnowledgeGrid({ knowledgeBases }: KnowledgeGridProps) {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
      {knowledgeBases.map((base) => (
        <KnowledgeBaseCard key={base.id} base={base} />
      ))}
    </div>
  );
}
