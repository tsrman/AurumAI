import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { Composer } from "@/components/aurum/chat/Composer";

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: true, isLoading: false }),
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));
vi.mock("@/hooks/useWorkflows", () => ({ useWorkflows: () => ({ data: [], isLoading: false }) }));
vi.mock("@/hooks/useChatAgents", () => ({ useChatAgents: () => ({ data: [] }) }));
vi.mock("@/hooks/useChatAgentDefinitions", () => ({
  useChatAgentDefinitions: () => ({
    data: [{ id: "def-hermes", name: "Finance analyst", modelId: "hermes-model", kind: "hermes" }],
  }),
}));
vi.mock("@/hooks/useDocuments", () => ({
  useAttachFile: () => ({ mutateAsync: vi.fn() }),
  useDocuments: () => ({ data: undefined }),
}));
vi.mock("@/lib/real-mode", () => ({ isRealMode: () => true }));

function renderWith(activeAgentId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AppShellProvider initialState={{ activeAgentId }}>
        <Composer />
      </AppShellProvider>
    </QueryClientProvider>,
  );
}

test("an administrator's Hermes agent takes workspace files such as ZIP archives", () => {
  renderWith("def-hermes");

  expect(screen.getByLabelText("Choose a file to attach")).toHaveAttribute(
    "accept",
    expect.stringContaining(".zip"),
  );
});

test("the ordinary chat does not offer the workspace file types", () => {
  renderWith("general");

  expect(screen.getByLabelText("Choose a file to attach").getAttribute("accept")).not.toContain(".zip");
});
