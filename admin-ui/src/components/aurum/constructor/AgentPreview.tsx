import { Badge } from "@/components/primitives/Badge";
import type { ConstructorState, Model } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface AgentPreviewProps {
  state: ConstructorState;
  models: Model[];
}

function makeBadge(name: string): string {
  const cleaned = name.trim();
  if (!cleaned) return "A";
  const initials = cleaned
    .split(/\s+/)
    .map((word) => word[0])
    .join("");
  return initials.slice(0, 2).toUpperCase() || "A";
}

export function AgentPreview({ state, models }: AgentPreviewProps) {
  const t = useTranslations("constructor.preview");
  const selectedTools = Object.keys(state.tools).filter((key) => state.tools[key]);
  const selectedBases = Object.keys(state.bases).filter((key) => state.bases[key]);
  const modelName = models.find((m) => m.id === state.model)?.shortName ?? state.model;

  const displayName = state.name.trim() || t("newAgent");
  const description = state.description.trim() || t("noDescription");

  return (
    <div className="sticky top-6 rounded-card border border-aurum-border bg-aurum-surface p-5 shadow-card">
      <div className="flex items-center gap-3">
        <Badge color="amber" className="text-sm px-3 py-1">
          {makeBadge(state.name)}
        </Badge>
        <div className="min-w-0">
          <h3 className="truncate text-lg font-semibold text-aurum-text">{displayName}</h3>
          <p className="text-xs text-aurum-text-3">{modelName}</p>
        </div>
      </div>

      <p className="mt-4 text-sm leading-relaxed text-aurum-text-2">{description}</p>

      <div className="mt-5 space-y-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-aurum-text-3">
            {t("tools")}
          </p>
          <div className="mt-1.5 flex flex-wrap gap-2">
            {selectedTools.length > 0 ? (
              selectedTools.map((tool) => (
                <span
                  key={tool}
                  className="rounded-chip border border-aurum-border bg-aurum-surface-2 px-2.5 py-1 text-xs text-aurum-text-2"
                >
                  {tool}
                </span>
              ))
            ) : (
              <span className="text-xs text-aurum-text-3">{t("noneSelected")}</span>
            )}
          </div>
        </div>

        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-aurum-text-3">
            {t("knowledgeBases")}
          </p>
          <div className="mt-1.5 flex flex-wrap gap-2">
            {selectedBases.length > 0 ? (
              selectedBases.map((base) => (
                <span
                  key={base}
                  className="rounded-chip border border-aurum-border bg-aurum-surface-2 px-2.5 py-1 text-xs text-aurum-text-2"
                >
                  {base}
                </span>
              ))
            ) : (
              <span className="text-xs text-aurum-text-3">{t("noneSelected")}</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
