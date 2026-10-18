import { Badge } from "@/components/primitives/Badge";
import { useAppShell } from "@/hooks/useAppShell";

export function AgentCard() {
  const { agents, activeAgentId } = useAppShell();
  const agent = agents.find((a) => a.id === activeAgentId) ?? agents[0];

  if (!agent) return null;

  return (
    <div className="flex flex-col items-center text-center">
      <div className="rounded-card border border-aurum-border bg-aurum-surface p-6 shadow-card sm:p-8">
        <div className="flex flex-col items-center gap-4">
          <Badge color={agent.badgeColor} className="text-sm px-3 py-1">
            {agent.badge}
          </Badge>
          <div>
            <h2 className="text-2xl font-semibold text-aurum-text">
              {agent.name}
            </h2>
            <p className="mt-1 text-sm text-aurum-text-3">{agent.kindLabel}</p>
          </div>
          <p className="max-w-md text-base text-aurum-text-2">
            {agent.description}
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            {agent.capabilities.map((capability) => (
              <span
                key={capability}
                className="rounded-chip border border-aurum-border bg-aurum-surface-2 px-2.5 py-1 text-xs font-medium text-aurum-text-2"
              >
                {capability}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
