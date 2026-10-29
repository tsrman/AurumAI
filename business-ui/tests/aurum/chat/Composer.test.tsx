import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { beforeEach, vi } from "vitest";
import { useAppShell } from "@/hooks/useAppShell";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { Composer } from "@/components/aurum/chat/Composer";
import * as chatApi from "@/lib/chat-api";

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: true, isLoading: false }),
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: undefined }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));
vi.mock("@/hooks/useWorkflows", () => ({
  useWorkflows: () => ({
    data: [
      {
        id: "workflow-1",
        name: "Research",
        description: "Parallel analysis",
        version: 1,
        maxParallelism: 2,
        status: "active",
      },
    ],
    isLoading: false,
  }),
}));

vi.mock("@/hooks/useChatAgents", () => ({
  useChatAgents: () => ({ data: [{ id: "hermes" }] }),
}));

interface AttachedDocument {
  id: string;
  title: string;
  status: string;
}

const mutateAsync = vi.fn();
const useDocumentsMock = vi.fn<
  (knowledgeBaseId: string, options?: { enabled?: boolean }) => {
    data: AttachedDocument[] | undefined;
  }
>();
vi.mock("@/hooks/useDocuments", () => ({
  useAttachFile: () => ({ mutateAsync }),
  useDocuments: (knowledgeBaseId: string, options?: { enabled?: boolean }) =>
    useDocumentsMock(knowledgeBaseId, options),
}));

beforeEach(() => {
  mutateAsync.mockReset();
  useDocumentsMock.mockReset();
  useDocumentsMock.mockReturnValue({ data: undefined });
});

function Wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <AppShellProvider>{children}</AppShellProvider>
    </QueryClientProvider>
  );
}

test("renders textarea", () => {
  render(<Composer />, { wrapper: Wrapper });
  expect(screen.getByRole("textbox")).toBeInTheDocument();
});

test("exposes the model picker with an accessible name", () => {
  render(<Composer />, { wrapper: Wrapper });

  expect(
    screen.getByRole("button", { name: "Choose model" })
  ).toHaveAttribute("aria-haspopup", "listbox");
});

test("send button is disabled when textarea is empty", () => {
  render(<Composer />, { wrapper: Wrapper });
  expect(screen.getByRole("button", { name: /Send/i })).toBeDisabled();
});

test("calls onSend when send button is clicked", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} />, { wrapper: Wrapper });

  const textarea = screen.getByRole("textbox");
  fireEvent.change(textarea, { target: { value: "Hello agent" } });

  const send = screen.getByRole("button", { name: /Send/i });
  expect(send).not.toBeDisabled();
  fireEvent.click(send);
  expect(onSend).toHaveBeenCalledTimes(1);
  expect(onSend).toHaveBeenCalledWith("Hello agent");
});

test("calls onSend on Enter without Shift", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} />, { wrapper: Wrapper });

  const textarea = screen.getByRole("textbox");
  fireEvent.change(textarea, { target: { value: "Hello agent" } });
  fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

  expect(onSend).toHaveBeenCalledTimes(1);
  expect(onSend).toHaveBeenCalledWith("Hello agent");
});

test("passes the selected workflow for one turn and then clears it", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} />, { wrapper: Wrapper });

  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));
  fireEvent.click(screen.getByRole("option", { name: /Research/ }));

  const textarea = screen.getByRole("textbox");
  fireEvent.change(textarea, { target: { value: "Build the report" } });
  fireEvent.click(screen.getByRole("button", { name: /Send/i }));

  expect(onSend).toHaveBeenCalledWith("Build the report", "workflow-1");
  expect(screen.getByRole("button", { name: "Choose workflow" })).toHaveTextContent(
    "Plain chat"
  );
});

test("keeps Hermes Agent chosen across turns of the conversation", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} />, { wrapper: Wrapper });

  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));
  fireEvent.click(screen.getByRole("option", { name: /Hermes Agent/ }));
  for (const text of ["Set up the repo", "Now run the tests"]) {
    fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
    fireEvent.click(screen.getByRole("button", { name: /Send/i }));
  }

  expect(onSend.mock.calls).toEqual([
    ["Set up the repo", undefined, undefined, "hermes"],
    ["Now run the tests", undefined, undefined, "hermes"],
  ]);
  expect(screen.getByRole("button", { name: "Choose workflow" })).toHaveTextContent(
    "Hermes Agent"
  );
});

test("another conversation starts in the ordinary chat", () => {
  const onSend = vi.fn();
  const { rerender } = render(<Composer onSend={onSend} conversationId="conversation-1" />, {
    wrapper: Wrapper,
  });
  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));
  fireEvent.click(screen.getByRole("option", { name: /Hermes Agent/ }));

  rerender(<Composer onSend={onSend} conversationId="conversation-2" />);

  expect(screen.getByRole("button", { name: "Choose workflow" })).toHaveTextContent(
    "Plain chat"
  );
});

function HermesConversation({ children }: { children: React.ReactNode }) {
  const { setConversations } = useAppShell();
  useEffect(() => {
    setConversations([
      {
        id: "conversation-1",
        agentId: "general",
        title: "Repo work",
        date: "Today",
        time: "10:00",
        messages: [],
        model: "gpt-4o-mini",
        temperature: 0.7,
        lastMessageAt: "2026-10-29T10:00:00Z",
        status: "active",
        messageCount: 2,
        agent: "hermes",
      },
    ]);
  }, [setConversations]);
  return <>{children}</>;
}

test("a conversation that runs on Hermes Agent reopens with it chosen", async () => {
  const onSend = vi.fn();
  render(
    <HermesConversation>
      <Composer onSend={onSend} conversationId="conversation-1" />
    </HermesConversation>,
    { wrapper: Wrapper }
  );

  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Choose workflow" })).toHaveTextContent("Hermes Agent")
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Run the tests again" } });
  fireEvent.click(screen.getByRole("button", { name: /Send/i }));

  expect(onSend.mock.calls).toEqual([["Run the tests again", undefined, undefined, "hermes"]]);
});

test("after a workflow turn a Hermes conversation is back on Hermes Agent", async () => {
  const onSend = vi.fn();
  render(
    <HermesConversation>
      <Composer onSend={onSend} conversationId="conversation-1" />
    </HermesConversation>,
    { wrapper: Wrapper }
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Choose workflow" })).toHaveTextContent("Hermes Agent")
  );

  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));
  fireEvent.click(screen.getByRole("option", { name: /Research/ }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Compare" } });
  fireEvent.click(screen.getByRole("button", { name: /Send/i }));

  expect(onSend.mock.calls[0]).toEqual(["Compare", "workflow-1"]);
  expect(screen.getByRole("button", { name: "Choose workflow" })).toHaveTextContent("Hermes Agent");
});

test("Shift+Enter does not call onSend", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} />, { wrapper: Wrapper });

  const textarea = screen.getByRole("textbox");
  fireEvent.change(textarea, { target: { value: "Hello agent" } });
  fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });

  expect(onSend).not.toHaveBeenCalled();
});

test("clicking send with empty text does not call onSend", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} />, { wrapper: Wrapper });

  const send = screen.getByRole("button", { name: /Send/i });
  fireEvent.click(send);

  expect(onSend).not.toHaveBeenCalled();
});

test("shows stop button instead of send while streaming", () => {
  const onStop = vi.fn();
  render(<Composer isStreaming onStop={onStop} />, { wrapper: Wrapper });

  expect(
    screen.queryByRole("button", { name: /Send/i })
  ).not.toBeInTheDocument();

  const stop = screen.getByRole("button", { name: /Stop generating/i });
  fireEvent.click(stop);
  expect(onStop).toHaveBeenCalledTimes(1);
});

test("Enter does not call onSend while streaming", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} isStreaming />, { wrapper: Wrapper });

  const textarea = screen.getByRole("textbox");
  fireEvent.change(textarea, { target: { value: "second message" } });
  fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

  expect(onSend).not.toHaveBeenCalled();
});

test("attach button uploads the selected file via ensureConversationId + useAttachFile", async () => {
  mutateAsync.mockResolvedValue({ id: "doc-1", knowledge_base_id: "kb-1" });
  const ensureConversationId = vi.fn().mockResolvedValue("conv-new");

  render(<Composer conversationId={null} ensureConversationId={ensureConversationId} />, {
    wrapper: Wrapper,
  });

  const file = new File(["# notes"], "notes.md", { type: "text/markdown" });
  const input = screen.getByLabelText("Choose a file to attach");
  fireEvent.change(input, { target: { files: [file] } });

  await waitFor(() => {
    expect(mutateAsync).toHaveBeenCalledWith({ conversationId: "conv-new", file });
  });
  expect(ensureConversationId).toHaveBeenCalledTimes(1);
});

test("attach rejects an unsupported file extension without calling ensureConversationId", async () => {
  const ensureConversationId = vi.fn();

  render(<Composer conversationId="conv-1" ensureConversationId={ensureConversationId} />, {
    wrapper: Wrapper,
  });

  const file = new File(["binary"], "payload.exe", { type: "application/octet-stream" });
  const input = screen.getByLabelText("Choose a file to attach");
  fireEvent.change(input, { target: { files: [file] } });

  expect(await screen.findByRole("alert")).toHaveTextContent("payload.exe");
  expect(ensureConversationId).not.toHaveBeenCalled();
  expect(mutateAsync).not.toHaveBeenCalled();
});

test("attach uploads every selected file sequentially into the same conversation", async () => {
  mutateAsync.mockImplementation(({ file }: { conversationId: string; file: File }) =>
    Promise.resolve({ id: `doc-${file.name}`, knowledge_base_id: "kb-1" })
  );
  const ensureConversationId = vi.fn().mockResolvedValue("conv-new");

  render(<Composer conversationId={null} ensureConversationId={ensureConversationId} />, {
    wrapper: Wrapper,
  });

  const fileA = new File(["a"], "a.md", { type: "text/markdown" });
  const fileB = new File(["b"], "b.txt", { type: "text/plain" });
  const input = screen.getByLabelText("Choose a file to attach");
  fireEvent.change(input, { target: { files: [fileA, fileB] } });

  await waitFor(() => {
    expect(mutateAsync).toHaveBeenCalledTimes(2);
  });
  // Every file in one selection lands in the *same* conversation —
  // ensureConversationId (which creates one) only runs once for the batch,
  // not once per file.
  expect(ensureConversationId).toHaveBeenCalledTimes(1);
  expect(mutateAsync).toHaveBeenNthCalledWith(1, { conversationId: "conv-new", file: fileA });
  expect(mutateAsync).toHaveBeenNthCalledWith(2, { conversationId: "conv-new", file: fileB });
});

test("attach uploads the valid files from a mixed selection and reports only the rejected one", async () => {
  mutateAsync.mockResolvedValue({ id: "doc-1", knowledge_base_id: "kb-1" });

  render(<Composer conversationId="conv-1" />, { wrapper: Wrapper });

  const good = new File(["ok"], "notes.md", { type: "text/markdown" });
  const bad = new File(["binary"], "payload.exe", { type: "application/octet-stream" });
  const input = screen.getByLabelText("Choose a file to attach");
  fireEvent.change(input, { target: { files: [good, bad] } });

  await waitFor(() => {
    expect(mutateAsync).toHaveBeenCalledWith({ conversationId: "conv-1", file: good });
  });
  expect(mutateAsync).toHaveBeenCalledTimes(1);
  expect(await screen.findByRole("alert")).toHaveTextContent("payload.exe");
});

test("shows an attachment chip once useDocuments reports the uploaded file", () => {
  useDocumentsMock.mockReturnValue({
    data: [{ id: "doc-1", title: "notes", status: "indexed" }],
  });

  render(<Composer conversationId="conv-1" />, { wrapper: Wrapper });

  expect(screen.getByText("notes")).toBeInTheDocument();
});

test("disabled prop blocks typing, Enter, and the send button", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} disabled />, { wrapper: Wrapper });

  const textarea = screen.getByRole("textbox");
  expect(textarea).toBeDisabled();

  fireEvent.change(textarea, { target: { value: "too early" } });
  fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
  expect(onSend).not.toHaveBeenCalled();

  expect(screen.getByRole("button", { name: /Send/i })).toBeDisabled();
});

test("opens the model settings dialog for the active conversation", async () => {
  render(<Composer conversationId="conv-1" />, { wrapper: Wrapper });

  fireEvent.click(screen.getByRole("button", { name: "Model settings" }));

  expect(
    await screen.findByRole("dialog", { name: "Model settings" })
  ).toBeInTheDocument();
});

test("model settings dialog lazily creates a conversation when none exists yet", async () => {
  const ensureConversationId = vi.fn().mockResolvedValue("conv-new");

  render(
    <Composer conversationId={null} ensureConversationId={ensureConversationId} />,
    { wrapper: Wrapper }
  );

  fireEvent.click(screen.getByRole("button", { name: "Model settings" }));

  expect(
    await screen.findByRole("dialog", { name: "Model settings" })
  ).toBeInTheDocument();
  expect(ensureConversationId).toHaveBeenCalledTimes(1);
});

test("closing the model settings dialog does not send a request", async () => {
  const updateSpy = vi.spyOn(chatApi, "updateConversation");
  render(<Composer conversationId="conv-1" />, { wrapper: Wrapper });

  fireEvent.click(screen.getByRole("button", { name: "Model settings" }));
  fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  );
  expect(updateSpy).not.toHaveBeenCalled();
});

test("active turn blocks an ordinary message and points to the question above", () => {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} blockedByActiveTurn />, { wrapper: Wrapper });

  const textarea = screen.getByRole("textbox", { name: "Message" });
  expect(textarea).toBeDisabled();
  expect(textarea).toHaveAttribute("placeholder", "Answer the question above to continue");
  fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
  expect(onSend).not.toHaveBeenCalled();
});
