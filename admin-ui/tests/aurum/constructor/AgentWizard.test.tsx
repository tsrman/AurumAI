import { render, screen, fireEvent, within, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { AgentWizard, wizardSteps } from "@/components/aurum/constructor/AgentWizard";
import { knowledgeBases, models } from "@/lib/mock-data";
import type { ConstructorState } from "@/types/aurum";

const skills = [
  { id: "skill-search", name: "Knowledge base search", description: "Finds documents", type: "tool" as const, inputSchema: {} },
  { id: "skill-web", name: "Web search", description: "Searches the internet", type: "tool" as const, inputSchema: {} },
];

const STATE: ConstructorState = {
  kind: "agent",
  name: "Analyst",
  description: "",
  model: "pro",
  prompt: "System prompt",
  temperature: 0.3,
  tools: { "skill-search": true, "skill-web": false },
  bases: Object.fromEntries(knowledgeBases.map((kb) => [kb.id, false])),
  webEnabled: true,
  imagesEnabled: true,
  askUserEnabled: true,
    hermesEgressMode: null,
    useOrgHermesModel: true,
    hermesSkillsScope: "all" as const,
};

describe("AgentWizard", () => {
  const setField = vi.fn();
  const toggleTool = vi.fn();
  const toggleBase = vi.fn();
  const save = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    save.mockResolvedValue(true);
  });

  function renderWizard(state: ConstructorState, extra: Partial<React.ComponentProps<typeof AgentWizard>> = {}) {
    return render(
      <AgentWizard
        state={state}
        setField={setField}
        toggleTool={toggleTool}
        toggleBase={toggleBase}
        models={models}
        skills={skills}
        knowledgeBases={knowledgeBases}
        isValid
        save={save}
        isSaving={false}
        saveError={null}
        {...extra}
      />,
    );
  }

  const next = () => fireEvent.click(screen.getByRole("button", { name: "Next" }));

  it("walks a new simple agent through its steps and creates it on the last one", () => {
    renderWizard(STATE);

    expect(screen.getByRole("radio", { name: /^Agent / })).toBeChecked();
    expect(screen.getByRole("button", { name: /4\. Tools and knowledge/ })).toBeDisabled();
    next();
    expect(screen.getByLabelText(/Agent name/i)).toBeInTheDocument();
    next();
    expect(screen.getByLabelText(/System prompt/i)).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: /Temperature/i })).toBeInTheDocument();
    next();
    expect(screen.getByRole("checkbox", { name: "Web search" })).toBeInTheDocument();
    next();
    expect(screen.getByText("Built-in capabilities")).toBeInTheDocument();
    next();
    expect(screen.getByText("Check the agent")).toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    expect(save).toHaveBeenCalled();
  });

  it("switches each built-in capability on its own", () => {
    renderWizard(STATE);
    for (let i = 0; i < 4; i++) next();

    fireEvent.click(screen.getByRole("checkbox", { name: "Web search and reading pages" }));
    expect(setField).toHaveBeenCalledWith("webEnabled", false);
    fireEvent.click(screen.getByRole("checkbox", { name: "Image generation and editing" }));
    expect(setField).toHaveBeenCalledWith("imagesEnabled", false);
    fireEvent.click(screen.getByRole("checkbox", { name: "Questions to the user" }));
    expect(setField).toHaveBeenCalledWith("askUserEnabled", false);
  });

  it("does not move on from the basics without a name", () => {
    renderWizard({ ...STATE, name: "" });
    next();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("lists the chosen capabilities in the review", () => {
    renderWizard({ ...STATE, imagesEnabled: false });
    for (let i = 0; i < 5; i++) next();

    const capabilities = screen.getAllByText("Built-in capabilities").find((el) => el.tagName === "DT")!.closest("div")!;
    expect(within(capabilities).getByText("Web search and reading pages, Questions to the user")).toBeInTheDocument();
  });

  it("toggles tools and knowledge bases on the tools step", () => {
    renderWizard(STATE);
    for (let i = 0; i < 3; i++) next();

    fireEvent.click(screen.getByRole("checkbox", { name: "Web search" }));
    expect(toggleTool).toHaveBeenCalledWith("skill-web");
    fireEvent.click(screen.getByRole("checkbox", { name: knowledgeBases[0].name }));
    expect(toggleBase).toHaveBeenCalledWith(knowledgeBases[0].id);
  });

  it("a Hermes Agent has the same steps and runs on the Hermes model by default", () => {
    expect(wizardSteps("hermes", false)).toEqual(wizardSteps("agent", false));
    renderWizard({ ...STATE, kind: "hermes" }, { hermesModel: "hermes-model" });
    next();
    expect(screen.getByText("Runs on the organization's Hermes model: hermes-model")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /Use the organization's Hermes model/ }));
    expect(setField).toHaveBeenCalledWith("useOrgHermesModel", false);
  });

  it("a Hermes Agent lists its skills only when it takes selected ones", () => {
    const hermes = { ...STATE, kind: "hermes" as const };
    const { unmount } = renderWizard(hermes, { hermesModel: "hermes-model" });
    next();
    next();
    next();
    expect(screen.getByRole("radio", { name: /All skills the user may run/ })).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "Web search" })).not.toBeInTheDocument();
    unmount();

    renderWizard({ ...hermes, hermesSkillsScope: "selected" }, { hermesModel: "hermes-model" });
    next();
    next();
    next();
    expect(screen.getByRole("checkbox", { name: "Web search" })).toBeInTheDocument();
  });

  it("a Hermes Agent can lower the internet mode", () => {
    renderWizard({ ...STATE, kind: "hermes" }, { hermesModel: "hermes-model" });
    next();
    next();
    next();
    next();
    fireEvent.click(screen.getByRole("radio", { name: /^None/ }));
    expect(setField).toHaveBeenCalledWith("hermesEgressMode", "none");
  });

  it("an existing agent skips the type step and can be saved from any step", () => {
    expect(wizardSteps("agent", true)[0]).toBe("basics");
    renderWizard(STATE, { kindLocked: true, submitLabel: "Save" });

    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /4\. Capabilities/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(save).toHaveBeenCalled();
  });

  it("shows a save error", () => {
    renderWizard(STATE, { saveError: "Dependency access denied" });
    expect(screen.getByText("Dependency access denied")).toBeInTheDocument();
  });

  it("checks whether a Hermes agent's own model calls tools", async () => {
    const checkModel = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    renderWizard({ ...STATE, kind: "hermes", useOrgHermesModel: false }, { checkModel, hermesModel: "hermes-model" });
    next();

    fireEvent.click(screen.getByRole("button", { name: "Check tool calling" }));
    expect(await screen.findByRole("status")).toHaveTextContent("did not call a tool");
    expect(checkModel).toHaveBeenCalledWith("pro");

    fireEvent.click(screen.getByRole("button", { name: "Check tool calling" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("native tool calls"));
  });
});
