"use client";

import { useState } from "react";
import { Button } from "@/components/primitives/Button";
import { Field } from "@/components/primitives/Field";
import { Slider } from "@/components/primitives/Slider";
import { Textarea } from "@/components/primitives/Textarea";
import { Toggle } from "@/components/primitives/Toggle";
import { BaseToggle } from "./BaseToggle";
import { ModelPickerField } from "./ModelPickerField";
import { ToolToggle } from "./ToolToggle";
import type { Skill } from "@/lib/api";
import type { ConstructorState, KnowledgeBase, Model } from "@/types/aurum";
import { useTranslations } from "next-intl";

export interface AgentWizardProps {
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

type Step = "kind" | "basics" | "instructions" | "tools" | "capabilities" | "review";

const card = "rounded-card border border-aurum-border bg-aurum-surface p-4 shadow-card";

/** Steps for the agent's type: a locked type is not asked again; Hermes brings its own tools. */
export function wizardSteps(kind: ConstructorState["kind"], kindLocked: boolean): Step[] {
  const steps: Step[] = kindLocked ? [] : ["kind"];
  steps.push("basics", "instructions");
  if (kind === "agent") steps.push("tools", "capabilities");
  steps.push("review");
  return steps;
}

/** Create or edit an agent step by step; the same steps edit an existing agent. */
export function AgentWizard({
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
}: AgentWizardProps) {
  const t = useTranslations("constructor.form");
  const w = useTranslations("constructor.wizard");
  const hermes = state.kind === "hermes";
  const steps = wizardSteps(state.kind, kindLocked);
  const [current, setCurrent] = useState<Step>(steps[0]);
  const index = Math.max(steps.indexOf(current), 0);
  const step = steps[index];
  const editing = kindLocked;

  const stepReady: Record<Step, boolean> = {
    kind: true,
    basics: state.name.trim().length > 0 && (hermes ? Boolean(hermesModel) : state.model.length > 0),
    instructions: state.prompt.trim().length > 0,
    tools: true,
    capabilities: true,
    review: isValid,
  };
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (step === "review" || editing) void save();
    else setCurrent(steps[index + 1]);
  };

  const capability = (key: "webEnabled" | "imagesEnabled" | "askUserEnabled", name: string, hint: string) => (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="text-sm font-medium text-aurum-text">{name}</p>
        <p className="text-xs text-aurum-text-3">{hint}</p>
      </div>
      <Toggle checked={state[key]} onChange={(value) => setField(key, value)} aria-label={name} />
    </div>
  );

  return (
    <form onSubmit={submit} className="space-y-4">
      <ol aria-label={w("steps")} className="flex flex-wrap gap-2">
        {steps.map((item, position) => (
          <li key={item}>
            <button
              type="button"
              aria-current={item === step ? "step" : undefined}
              disabled={!editing && position > index}
              onClick={() => setCurrent(item)}
              className={`rounded-btn border px-3 py-1.5 text-xs font-medium ${
                item === step
                  ? "border-aurum-accent bg-aurum-accent-soft text-aurum-text"
                  : "border-aurum-border text-aurum-text-2 disabled:opacity-50"
              }`}
            >
              {position + 1}. {w(`steps_${item}`)}
            </button>
          </li>
        ))}
      </ol>

      {step === "kind" ? (
        <section className={card}>
          <p className="mb-3 text-sm font-medium text-aurum-text-2">{t("kind")}</p>
          {(["agent", "hermes"] as const).map((kind) => (
            <label key={kind} className="mb-2 flex cursor-pointer items-start gap-3 text-sm text-aurum-text">
              <input
                type="radio"
                name="agent-kind"
                checked={state.kind === kind}
                onChange={() => setField("kind", kind)}
              />
              <span>
                <span className="font-medium">{w(`kind_${kind}`)}</span>
                <span className="block text-xs text-aurum-text-3">{w(`kind_${kind}_hint`)}</span>
              </span>
            </label>
          ))}
        </section>
      ) : null}

      {step === "basics" ? (
        <>
          <section className={card}>
            <Field label={t("name")} placeholder={t("namePlaceholder")} value={state.name} onChange={(e) => setField("name", e.target.value)} required />
          </section>
          <section className={card}>
            <Field label={t("description")} placeholder={t("descriptionPlaceholder")} value={state.description} onChange={(e) => setField("description", e.target.value)} />
          </section>
          <section className={card}>
            <p className="mb-3 text-sm font-medium text-aurum-text-2">{t("model")}</p>
            {hermes ? (
              <p className="text-sm text-aurum-text-2">
                {hermesModel ? t("hermesModel", { model: hermesModel }) : t("hermesModelUnset")}
              </p>
            ) : (
              <ModelPickerField value={state.model} options={models} onChange={(modelId) => setField("model", modelId)} />
            )}
          </section>
        </>
      ) : null}

      {step === "instructions" ? (
        <>
          <section className={card}>
            <Textarea label={t("systemPrompt")} rows={8} value={state.prompt} onChange={(e) => setField("prompt", e.target.value)} />
          </section>
          {hermes ? null : (
            <section className={card}>
              <div className="mb-3 flex items-center justify-between">
                <label htmlFor="temperature" className="text-sm font-medium text-aurum-text-2">{t("temperature")}</label>
                {state.temperature !== null ? (
                  <span className="text-sm tabular-nums text-aurum-text">{state.temperature.toFixed(1)}</span>
                ) : null}
              </div>
              <label className="mb-3 flex items-center gap-2 text-sm text-aurum-text-2">
                <input type="checkbox" checked={state.temperature === null} onChange={(event) => setField("temperature", event.target.checked ? null : 0.3)} />
                {t("inherit")}
              </label>
              {state.temperature !== null ? (
                <Slider id="temperature" min={0} max={2} step={0.1} value={state.temperature} onChange={(value) => setField("temperature", value)} />
              ) : null}
            </section>
          )}
        </>
      ) : null}

      {step === "tools" ? (
        <>
          <section className={card}>
            <h3 className="mb-3 text-sm font-medium text-aurum-text-2">{t("tools")}</h3>
            <div className="space-y-4">
              {skills.length === 0 ? <p className="text-xs text-aurum-text-3">{t("noTools")}</p> : null}
              {skills.map((skill) => (
                <ToolToggle key={skill.id} name={skill.name} description={skill.description ?? ""} checked={state.tools[skill.id] ?? false} onChange={() => toggleTool(skill.id)} />
              ))}
            </div>
          </section>
          <section className={card}>
            <h3 className="mb-3 text-sm font-medium text-aurum-text-2">{t("knowledgeBases")}</h3>
            <div className="space-y-4">
              {knowledgeBases.length === 0 ? <p className="text-xs text-aurum-text-3">{t("noKnowledgeBases")}</p> : null}
              {knowledgeBases.map((kb) => (
                <BaseToggle key={kb.id} name={kb.name} description={kb.description} checked={state.bases[kb.id] ?? false} onChange={() => toggleBase(kb.id)} />
              ))}
            </div>
          </section>
        </>
      ) : null}

      {step === "capabilities" ? (
        <section className={card}>
          <h3 className="mb-1 text-sm font-medium text-aurum-text-2">{w("capabilities")}</h3>
          <p className="mb-4 text-xs text-aurum-text-3">{w("capabilitiesHint")}</p>
          <div className="space-y-4">
            {capability("webEnabled", w("web"), w("webHint"))}
            {capability("imagesEnabled", w("images"), w("imagesHint"))}
            {capability("askUserEnabled", w("askUser"), w("askUserHint"))}
          </div>
        </section>
      ) : null}

      {step === "review" ? (
        <section className={card}>
          <h3 className="mb-3 text-sm font-medium text-aurum-text-2">{w("review")}</h3>
          <dl className="space-y-2 text-sm">
            <div><dt className="text-xs text-aurum-text-3">{t("kind")}</dt><dd>{w(`kind_${state.kind}`)}</dd></div>
            <div><dt className="text-xs text-aurum-text-3">{t("name")}</dt><dd>{state.name || "—"}</dd></div>
            <div><dt className="text-xs text-aurum-text-3">{t("model")}</dt><dd>{hermes ? (hermesModel ?? "—") : state.model || "—"}</dd></div>
            {hermes ? null : (
              <>
                <div><dt className="text-xs text-aurum-text-3">{t("tools")}</dt><dd>{Object.values(state.tools).filter(Boolean).length}</dd></div>
                <div><dt className="text-xs text-aurum-text-3">{t("knowledgeBases")}</dt><dd>{Object.values(state.bases).filter(Boolean).length}</dd></div>
                <div>
                  <dt className="text-xs text-aurum-text-3">{w("capabilities")}</dt>
                  <dd>
                    {[
                      state.webEnabled ? w("web") : null,
                      state.imagesEnabled ? w("images") : null,
                      state.askUserEnabled ? w("askUser") : null,
                    ].filter(Boolean).join(", ") || w("none")}
                  </dd>
                </div>
              </>
            )}
          </dl>
        </section>
      ) : null}

      {saveError ? (
        <div role="alert" className="rounded-card bg-aurum-err-bg p-4 text-sm text-aurum-err-fg">{saveError}</div>
      ) : null}

      <div className="flex items-center justify-between gap-3 pt-2">
        <div>
          {onCancel ? (
            <Button variant="secondary" onClick={onCancel}>{t("cancel")}</Button>
          ) : null}
        </div>
        <div className="flex items-center gap-3">
          {index > 0 ? (
            <Button variant="secondary" onClick={() => setCurrent(steps[index - 1])}>{w("back")}</Button>
          ) : null}
          {step !== "review" ? (
            <Button type={editing ? "button" : "submit"} variant={editing ? "secondary" : "primary"} disabled={!stepReady[step]} onClick={editing ? () => setCurrent(steps[index + 1]) : undefined}>
              {w("next")}
            </Button>
          ) : null}
          {step === "review" || editing ? (
            <Button type={editing && step !== "review" ? "button" : "submit"} onClick={editing && step !== "review" ? () => void save() : undefined} disabled={!isValid || isSaving}>
              {isSaving ? t("saving") : (submitLabel ?? t("create"))}
            </Button>
          ) : null}
        </div>
      </div>
    </form>
  );
}
