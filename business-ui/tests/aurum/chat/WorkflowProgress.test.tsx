import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { WorkflowProgress } from "@/components/aurum/chat/WorkflowProgress";
import type { WorkflowProgressState } from "@/types/aurum";
import { decideWorkflowNode } from "@/lib/workflows-api";

vi.mock("@/lib/workflows-api", () => ({
  decideWorkflowNode: vi.fn(),
}));

const progress: WorkflowProgressState = {
  workflowRunId: "run-1",
  status: "running",
  nodes: {
    research: {
      nodeKey: "research",
      nodeName: "Research",
      status: "completed",
    },
    report: {
      nodeKey: "report",
      nodeName: "Report",
      status: "running",
    },
  },
};

test("renders workflow phase and sanitized node progress", () => {
  render(<WorkflowProgress progress={progress} />);
  expect(screen.getByText("Running workflow")).toBeInTheDocument();
  expect(screen.getByText("Research")).toBeInTheDocument();
  expect(screen.getByText("Report")).toBeInTheDocument();
  expect(screen.getByText("Done")).toBeInTheDocument();
  expect(screen.getAllByText("Running").length).toBeGreaterThan(0);
});

test("renders a skipped node distinctly from a failed one", () => {
  render(
    <WorkflowProgress
      progress={{
        workflowRunId: "run-1",
        status: "completed",
        nodes: {
          fallback: {
            nodeKey: "fallback",
            nodeName: "Fallback step",
            status: "completed",
          },
          "on-success-branch": {
            nodeKey: "on-success-branch",
            nodeName: "Success branch",
            status: "skipped",
          },
        },
      }}
    />
  );
  expect(screen.getByText("Skipped — condition not met")).toBeInTheDocument();
});

test("localizes stable error codes and never renders the raw code", () => {
  render(
    <WorkflowProgress
      progress={{
        workflowRunId: "run-1",
        status: "failed",
        errorCode: "access_revoked",
        nodes: {},
      }}
    />
  );
  expect(screen.getByText("Access to the workflow was revoked")).toBeInTheDocument();
  expect(screen.queryByText("access_revoked")).not.toBeInTheDocument();
});

test("uses a generic message for unknown error codes", () => {
  render(
    <WorkflowProgress
      progress={{
        workflowRunId: "run-1",
        status: "failed",
        errorCode: "internal_secret_failure",
        nodes: {},
      }}
    />
  );
  expect(screen.getByText("The workflow could not be run")).toBeInTheDocument();
  expect(screen.queryByText("internal_secret_failure")).not.toBeInTheDocument();
});

// The label map previously keyed on "result_too_large", a code nothing emits,
// so the two size/availability failures that actually reach the UI both fell
// through to the generic message.
test.each([
  ["output_limit_exceeded", "The workflow result exceeded the allowed size"],
  ["workflow_unavailable", "The workflow is unavailable right now"],
])("localizes the %s code emitted by the runner", (errorCode, label) => {
  render(
    <WorkflowProgress
      progress={{
        workflowRunId: "run-1",
        status: "failed",
        errorCode,
        nodes: {},
      }}
    />
  );
  expect(screen.getByText(label)).toBeInTheDocument();
  expect(screen.queryByText(errorCode)).not.toBeInTheDocument();
});

test("shows approve/reject controls for a node waiting on human confirmation", async () => {
  vi.mocked(decideWorkflowNode).mockResolvedValue(undefined);
  render(
    <WorkflowProgress
      progress={{
        workflowRunId: "run-1",
        status: "running",
        nodes: {
          publish: {
            nodeKey: "publish",
            nodeName: "Publishing",
            status: "waiting_human",
          },
        },
      }}
    />
  );
  expect(screen.getByText("Publishing")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "publish: approve" }));
  await waitFor(() => {
    expect(decideWorkflowNode).toHaveBeenCalledWith("run-1", "publish", "approve");
  });
  expect(screen.getByText("Decision sent")).toBeInTheDocument();
});

test("shows an inline error and allows retry when the decision request fails", async () => {
  vi.mocked(decideWorkflowNode).mockRejectedValueOnce(new Error("network"));
  vi.mocked(decideWorkflowNode).mockResolvedValueOnce(undefined);
  render(
    <WorkflowProgress
      progress={{
        workflowRunId: "run-1",
        status: "running",
        nodes: {
          publish: {
            nodeKey: "publish",
            nodeName: "Publishing",
            status: "waiting_human",
          },
        },
      }}
    />
  );
  fireEvent.click(screen.getByRole("button", { name: "publish: reject" }));
  await waitFor(() => {
    expect(screen.getByText("Could not send, please try again")).toBeInTheDocument();
  });
  fireEvent.click(screen.getByRole("button", { name: "publish: reject" }));
  await waitFor(() => {
    expect(screen.getByText("Decision sent")).toBeInTheDocument();
  });
});
