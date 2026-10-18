import { render, screen } from "@testing-library/react";

import { WorkflowDiagnostics } from "@/components/workflows/WorkflowDiagnostics";
import type { WorkflowRunDiagnostic } from "@/lib/api";

const diagnostic: WorkflowRunDiagnostic = {
  id: "run-1",
  workflowDefinitionId: "workflow-1",
  workflowDefinitionVersion: 2,
  conversationId: "conversation-1",
  turnId: "turn-1",
  status: "failed",
  errorCode: "access_revoked",
  internalError: null,
  createdAt: "2026-09-02T00:00:00Z",
  startedAt: null,
  completedAt: null,
  nodes: [
    {
      phaseKey: "research",
      agentDefinitionId: "agent-1",
      agentName: "Research",
      status: "completed",
      attemptCount: 1,
      errorCode: null,
      startedAt: null,
      completedAt: null,
      output: null,
      internalError: null,
      diagnosticExpiresAt: "2026-09-01T00:00:00Z",
      outputExpired: true,
    },
  ],
};

test("renders an explicit retention message for expired output", () => {
  render(<WorkflowDiagnostics diagnostic={diagnostic} />);

  expect(screen.getByText("Removed by the retention policy")).toBeInTheDocument();
  expect(screen.getByText("Error: access_revoked")).toBeInTheDocument();
});
