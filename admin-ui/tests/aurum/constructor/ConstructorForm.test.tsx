import { render, screen, fireEvent } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { ConstructorForm } from "@/components/aurum/constructor/ConstructorForm";
import { knowledgeBases, models } from "@/lib/mock-data";
import type { ConstructorState } from "@/types/aurum";

const skills = [
  {
    id: "skill-search",
    name: "Knowledge base search",
    description: "Finds documents",
    type: "tool" as const,
    inputSchema: {},
  },
  {
    id: "skill-web",
    name: "Web search",
    description: "Searches the internet",
    type: "tool" as const,
    inputSchema: {},
  },
];

const DEFAULT_STATE: ConstructorState = {
  kind: "agent",
  name: "",
  description: "",
  model: "pro",
  prompt: "System prompt",
  temperature: 0.3,
  tools: {
    "skill-search": true,
    "skill-web": false,
  },
  bases: Object.fromEntries(knowledgeBases.map((kb) => [kb.id, false])),
};

describe("ConstructorForm", () => {
  const setField = vi.fn();
  const toggleTool = vi.fn();
  const toggleBase = vi.fn();
  const save = vi.fn();

  beforeEach(() => {
    setField.mockClear();
    toggleTool.mockClear();
    toggleBase.mockClear();
    save.mockClear();
    save.mockResolvedValue(true);
  });

  function renderForm(state: ConstructorState, isValid = false) {
    return render(
      <ConstructorForm
        state={state}
        setField={setField}
        toggleTool={toggleTool}
        toggleBase={toggleBase}
        models={models}
        skills={skills}
        knowledgeBases={knowledgeBases}
        isValid={isValid}
        save={save}
        isSaving={false}
        saveError={null}
      />
    );
  }

  it("renders name, description, prompt and temperature fields", () => {
    renderForm(DEFAULT_STATE);

    expect(screen.getByLabelText(/Agent name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Short description/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/System prompt/i)).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: /Temperature/i })).toBeInTheDocument();
  });

  it("renders the active model on the picker trigger and lists all options when opened", () => {
    renderForm(DEFAULT_STATE);

    const trigger = screen.getByRole("button", { name: models[0].shortName });
    fireEvent.click(trigger);

    for (const model of models) {
      expect(
        screen.getByRole("option", {
          name: (accessibleName: string) => accessibleName.includes(model.name),
        })
      ).toBeInTheDocument();
    }
  });

  it("renders tool and knowledge base toggles", () => {
    renderForm(DEFAULT_STATE);

    for (const skill of skills) {
      expect(screen.getByRole("checkbox", { name: skill.name })).toBeInTheDocument();
    }

    for (const kb of knowledgeBases) {
      expect(screen.getByRole("checkbox", { name: kb.name })).toBeInTheDocument();
    }
  });

  it("calls setField when name or description changes", () => {
    renderForm(DEFAULT_STATE);

    fireEvent.change(screen.getByLabelText(/Agent name/i), {
      target: { value: "Analyst" },
    });
    expect(setField).toHaveBeenCalledWith("name", "Analyst");

    fireEvent.change(screen.getByLabelText(/Short description/i), {
      target: { value: "Description" },
    });
    expect(setField).toHaveBeenCalledWith("description", "Description");
  });

  it("calls toggleTool and toggleBase when switches change", () => {
    renderForm(DEFAULT_STATE);

    fireEvent.click(screen.getByRole("checkbox", { name: "Web search" }));
    expect(toggleTool).toHaveBeenCalledWith("skill-web");

    fireEvent.click(screen.getByRole("checkbox", { name: knowledgeBases[0].name }));
    expect(toggleBase).toHaveBeenCalledWith(knowledgeBases[0].id);
  });

  it("calls save on form submit", () => {
    renderForm({ ...DEFAULT_STATE, name: "Agent" }, true);

    fireEvent.click(screen.getByRole("button", { name: /Create agent/i }));
    expect(save).toHaveBeenCalled();
  });

  it("disables submit when invalid", () => {
    renderForm(DEFAULT_STATE, false);

    expect(screen.getByRole("button", { name: /Create agent/i })).toBeDisabled();
  });

  it("shows a persistence error instead of a draft-only warning", () => {
    render(
      <ConstructorForm
        state={DEFAULT_STATE}
        setField={setField}
        toggleTool={toggleTool}
        toggleBase={toggleBase}
        models={models}
        skills={skills}
        knowledgeBases={knowledgeBases}
        isValid
        save={save}
        isSaving={false}
        saveError="Dependency access denied"
      />
    );

    expect(screen.getByText("Dependency access denied")).toBeInTheDocument();
    expect(screen.queryByText(/does not save the agent on the server/i)).not.toBeInTheDocument();
  });

  it("a Hermes Agent runs on the organization's Hermes model, without tools or knowledge bases", () => {
    render(
      <ConstructorForm
        state={{ ...DEFAULT_STATE, kind: "hermes" }}
        setField={setField}
        toggleTool={toggleTool}
        toggleBase={toggleBase}
        models={models}
        skills={skills}
        knowledgeBases={knowledgeBases}
        hermesModel="hermes-model"
        isValid
        save={save}
        isSaving={false}
        saveError={null}
      />
    );

    expect(screen.getByText("Runs on the organization's Hermes model: hermes-model")).toBeInTheDocument();
    expect(screen.queryByText("Tools")).not.toBeInTheDocument();
    expect(screen.queryByText("Knowledge bases")).not.toBeInTheDocument();
    expect(screen.queryByText("Web search")).not.toBeInTheDocument();
  });

  it("the type is fixed once the agent exists", () => {
    render(
      <ConstructorForm
        state={DEFAULT_STATE}
        setField={setField}
        toggleTool={toggleTool}
        toggleBase={toggleBase}
        models={models}
        skills={skills}
        knowledgeBases={knowledgeBases}
        kindLocked
        isValid
        save={save}
        isSaving={false}
        saveError={null}
      />
    );

    expect(screen.getByLabelText("Type")).toBeDisabled();
  });
});
