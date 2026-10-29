import { Button } from "@/components/primitives/Button";
import { Field } from "@/components/primitives/Field";
import { Slider } from "@/components/primitives/Slider";
import { Textarea } from "@/components/primitives/Textarea";
import { BaseToggle } from "./BaseToggle";
import { ModelPickerField } from "./ModelPickerField";
import { ToolToggle } from "./ToolToggle";
import type { Skill } from "@/lib/api";
import type { ConstructorState, KnowledgeBase, Model } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface ConstructorFormProps {
  state: ConstructorState;
  setField: (
    name: keyof ConstructorState,
    value: ConstructorState[keyof ConstructorState],
  ) => void;
  toggleTool: (toolName: string) => void;
  toggleBase: (baseName: string) => void;
  models: Model[];
  skills: Skill[];
  knowledgeBases: KnowledgeBase[];
  /** The organization's Hermes model; null when Hermes has none. */
  hermesModel?: string | null;
  /** The type is fixed once the agent exists. */
  kindLocked?: boolean;
  isValid: boolean;
  save: () => Promise<boolean>;
  isSaving: boolean;
  saveError: string | null;
  /** Defaults to the create label. */
  submitLabel?: string;
  onCancel?: () => void;
}

export function ConstructorForm({
  state,
  setField,
  toggleTool,
  toggleBase,
  models,
  skills,
  knowledgeBases,
  hermesModel = null,
  kindLocked = false,
  isValid,
  save,
  isSaving,
  saveError,
  submitLabel,
  onCancel,
}: ConstructorFormProps) {
  const t = useTranslations("constructor.form");
  const hermes = state.kind === "hermes";
  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void save();
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <section className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
        <label
          htmlFor="agent-kind"
          className="mb-2 block text-sm font-medium text-aurum-text-2"
        >
          {t("kind")}
        </label>
        <select
          id="agent-kind"
          value={state.kind}
          disabled={kindLocked}
          onChange={(event) =>
            setField("kind", event.target.value as ConstructorState["kind"])
          }
          className="w-full rounded-btn border border-aurum-border bg-aurum-surface px-3 py-2 text-sm text-aurum-text disabled:opacity-60"
        >
          <option value="agent">{t("kinds.agent")}</option>
          <option value="hermes">{t("kinds.hermes")}</option>
        </select>
        {hermes ? (
          <p className="mt-2 text-xs leading-5 text-aurum-text-3">
            {t("hermesHint")}
          </p>
        ) : null}
      </section>

      <section className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
        <Field
          label={t("name")}
          placeholder={t("namePlaceholder")}
          value={state.name}
          onChange={(e) => setField("name", e.target.value)}
          required
        />
      </section>

      <section className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
        <Field
          label={t("description")}
          placeholder={t("descriptionPlaceholder")}
          value={state.description}
          onChange={(e) => setField("description", e.target.value)}
        />
      </section>

      <section className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
        <p className="mb-3 text-sm font-medium text-aurum-text-2">
          {t("model")}
        </p>
        {hermes ? (
          <p className="text-sm text-aurum-text-2">
            {hermesModel
              ? t("hermesModel", { model: hermesModel })
              : t("hermesModelUnset")}
          </p>
        ) : (
          <ModelPickerField
            value={state.model}
            options={models}
            onChange={(modelId) => setField("model", modelId)}
          />
        )}
      </section>

      <section className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
        <Textarea
          label={t("systemPrompt")}
          rows={5}
          value={state.prompt}
          onChange={(e) => setField("prompt", e.target.value)}
        />
      </section>

      {hermes ? null : (
        <>
          <section className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
            <h3 className="mb-3 text-sm font-medium text-aurum-text-2">
              {t("tools")}
            </h3>
            <div className="space-y-4">
              {skills.length === 0 ? (
                <p className="text-xs text-aurum-text-3">{t("noTools")}</p>
              ) : null}
              {skills.map((skill) => (
                <ToolToggle
                  key={skill.id}
                  name={skill.name}
                  description={skill.description ?? ""}
                  checked={state.tools[skill.id] ?? false}
                  onChange={() => toggleTool(skill.id)}
                />
              ))}
            </div>
          </section>

          <section className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
            <h3 className="mb-3 text-sm font-medium text-aurum-text-2">
              {t("knowledgeBases")}
            </h3>
            <div className="space-y-4">
              {knowledgeBases.length === 0 ? (
                <p className="text-xs text-aurum-text-3">
                  {t("noKnowledgeBases")}
                </p>
              ) : null}
              {knowledgeBases.map((kb) => (
                <BaseToggle
                  key={kb.id}
                  name={kb.name}
                  description={kb.description}
                  checked={state.bases[kb.id] ?? false}
                  onChange={() => toggleBase(kb.id)}
                />
              ))}
            </div>
          </section>

          <section className="rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card">
            <div className="mb-3 flex items-center justify-between">
              <label
                htmlFor="temperature"
                className="text-sm font-medium text-aurum-text-2"
              >
                {t("temperature")}
              </label>
              {state.temperature !== null ? (
                <span className="text-sm tabular-nums text-aurum-text">
                  {state.temperature.toFixed(1)}
                </span>
              ) : null}
            </div>
            <label className="mb-3 flex items-center gap-2 text-sm text-aurum-text-2">
              <input
                type="checkbox"
                checked={state.temperature === null}
                onChange={(event) =>
                  setField("temperature", event.target.checked ? null : 0.3)
                }
              />
              {t("inherit")}
            </label>
            {state.temperature !== null ? (
              <Slider
                id="temperature"
                min={0}
                max={2}
                step={0.1}
                value={state.temperature}
                onChange={(value) => setField("temperature", value)}
              />
            ) : null}
          </section>
        </>
      )}

      {saveError ? (
        <div
          role="alert"
          className="rounded-card bg-aurum-err-bg p-4 text-sm text-aurum-err-fg"
        >
          {saveError}
        </div>
      ) : null}

      <div className="flex items-center justify-end gap-3 pt-2">
        {onCancel ? (
          <Button variant="secondary" onClick={onCancel}>
            {t("cancel")}
          </Button>
        ) : null}
        <Button type="submit" disabled={!isValid || isSaving}>
          {isSaving ? t("saving") : (submitLabel ?? t("create"))}
        </Button>
      </div>
    </form>
  );
}
