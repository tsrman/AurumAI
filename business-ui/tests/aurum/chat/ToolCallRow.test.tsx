import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { ToolCallRow } from "@/components/aurum/chat/ToolCallRow";
import { toolCallsFromHistory } from "@/lib/chat-api";

test("labels a Hermes tool by its kind and shows no detail", () => {
  render(
    <ToolCallRow
      tool={{
        callId: "hermes-1-0",
        toolId: "hermes.terminal",
        name: "Running commands",
        detail: '{"tool":"hermes.terminal","status":"completed"}',
        state: "done",
      }}
    />
  );

  expect(screen.getByText("Running commands")).toBeInTheDocument();
  expect(screen.queryByText(/status/)).not.toBeInTheDocument();
});

test("an unknown Hermes kind falls back to a generic label", () => {
  render(<ToolCallRow tool={{ toolId: "hermes.surprise", name: "x", detail: "", state: "run" }} />);

  expect(screen.getByText("Using a tool")).toBeInTheDocument();
});

test("a reloaded Hermes turn keeps its tool ids", () => {
  const [tool] = toolCallsFromHistory(
    [{ call_id: "hermes-1-0", tool: "hermes.files", label: "Working with files" }],
    { "hermes-1-0": { tool: "hermes.files", status: "completed" } }
  );

  expect(tool.toolId).toBe("hermes.files");
  render(<ToolCallRow tool={tool} />);
  expect(screen.getByText("Working with files")).toBeInTheDocument();
});
