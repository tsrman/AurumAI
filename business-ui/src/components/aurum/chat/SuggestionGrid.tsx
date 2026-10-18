import { useAppShell } from "@/hooks/useAppShell";

export interface SuggestionGridProps {
  onSuggestion?: (text: string) => void;
}

export function SuggestionGrid({ onSuggestion }: SuggestionGridProps) {
  const { agents, activeAgentId } = useAppShell();
  const agent = agents.find((a) => a.id === activeAgentId) ?? agents[0];

  if (!agent) return null;

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {agent.suggestions.map((suggestion) => (
        <button
          key={suggestion}
          type="button"
          onClick={() => onSuggestion?.(suggestion)}
          className="rounded-card border border-aurum-border bg-aurum-surface p-4 text-left text-sm text-aurum-text-2 shadow-card transition hover:border-aurum-accent hover:bg-aurum-accent-soft hover:text-aurum-text"
        >
          {suggestion}
        </button>
      ))}
    </div>
  );
}
