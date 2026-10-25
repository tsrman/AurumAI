import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, vi } from "vitest";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { Composer } from "@/components/aurum/chat/Composer";

let models: Array<{ id: string; vision?: boolean }> = [];

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: true, isLoading: false }),
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: { data: models } }) }));
vi.mock("@/hooks/useSettings", () => ({ useSettings: () => ({ data: undefined }) }));
vi.mock("@/hooks/useWorkflows", () => ({
  useWorkflows: () => ({
    data: [
      {
        id: "workflow-1",
        name: "Illustrate",
        description: "Draw and refine",
        version: 1,
        maxParallelism: 1,
        status: "active",
      },
    ],
    isLoading: false,
  }),
}));
vi.mock("@/hooks/useDocuments", () => ({
  useAttachFile: () => ({ mutateAsync: vi.fn() }),
  useDocuments: () => ({ data: undefined }),
}));

const uploadConversationImage = vi.fn();
const deleteConversationImage = vi.fn();
vi.mock("@/lib/images-api", () => ({
  uploadConversationImage: (...args: unknown[]) => uploadConversationImage(...args),
  deleteConversationImage: (...args: unknown[]) => deleteConversationImage(...args),
  getConversationImageBlob: vi.fn(),
  imageIdsFrom: () => [],
}));

beforeEach(() => {
  models = [{ id: "vision-model", vision: true }];
  uploadConversationImage.mockReset();
  deleteConversationImage.mockReset();
  deleteConversationImage.mockResolvedValue(undefined);
  URL.createObjectURL = vi.fn(() => "blob:preview");
  URL.revokeObjectURL = vi.fn();
});

function Wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={queryClient}>
      <AppShellProvider>{children}</AppShellProvider>
    </QueryClientProvider>
  );
}

const png = () => new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "cat.png", { type: "image/png" });

test("a vision model takes images: upload, preview, then send with the ids", async () => {
  uploadConversationImage.mockResolvedValue({ imageId: "image-1", contentType: "image/png" });
  const onSend = vi.fn();
  render(<Composer conversationId="conv-1" onSend={onSend} />, { wrapper: Wrapper });

  const input = screen.getByLabelText("Choose a file to attach");
  expect(input).toHaveAttribute("accept", expect.stringContaining(".png"));
  fireEvent.change(input, { target: { files: [png()] } });

  expect(await screen.findByRole("img", { name: "cat.png" })).toHaveAttribute("src", "blob:preview");
  expect(uploadConversationImage).toHaveBeenCalledWith("conv-1", expect.any(File));
  // An image alone is enough to send.
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(onSend).toHaveBeenCalledWith("", undefined, ["image-1"]);
  expect(screen.queryByRole("img", { name: "cat.png" })).not.toBeInTheDocument();
});

test("a removed image is not sent", async () => {
  uploadConversationImage.mockResolvedValue({ imageId: "image-1", contentType: "image/png" });
  const onSend = vi.fn();
  render(<Composer conversationId="conv-1" onSend={onSend} />, { wrapper: Wrapper });

  fireEvent.change(screen.getByLabelText("Choose a file to attach"), { target: { files: [png()] } });
  fireEvent.click(await screen.findByRole("button", { name: "Remove cat.png" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "hello" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));

  expect(onSend).toHaveBeenCalledWith("hello");
  expect(deleteConversationImage).toHaveBeenCalledWith("conv-1", "image-1");
});

test("other models refuse images before uploading", async () => {
  models = [{ id: "text-model", vision: false }];
  render(<Composer conversationId="conv-1" />, { wrapper: Wrapper });

  const input = screen.getByLabelText("Choose a file to attach");
  expect(input).not.toHaveAttribute("accept", expect.stringContaining(".png"));
  fireEvent.change(input, { target: { files: [png()] } });

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "cat.png: the selected model does not accept images"
  );
  expect(uploadConversationImage).not.toHaveBeenCalled();
});

test("at most four images go with one message", async () => {
  let next = 0;
  uploadConversationImage.mockImplementation(async () => ({ imageId: `image-${++next}` }));
  render(<Composer conversationId="conv-1" />, { wrapper: Wrapper });

  const files = Array.from({ length: 5 }, (_, index) =>
    new File([new Uint8Array([1])], `p${index}.png`, { type: "image/png" })
  );
  fireEvent.change(screen.getByLabelText("Choose a file to attach"), { target: { files } });

  await waitFor(() => expect(uploadConversationImage).toHaveBeenCalledTimes(4));
  expect(await screen.findByRole("alert")).toHaveTextContent("At most 4 images per message");
});


test("a selected workflow takes images even when the chat model does not", async () => {
  models = [{ id: "text-model", vision: false }];
  uploadConversationImage.mockResolvedValue({ imageId: "image-1", contentType: "image/png" });
  const onSend = vi.fn();
  render(<Composer conversationId="conv-1" onSend={onSend} />, { wrapper: Wrapper });

  fireEvent.click(screen.getByRole("button", { name: "Choose workflow" }));
  fireEvent.click(screen.getByRole("option", { name: /Illustrate/ }));
  const input = screen.getByLabelText("Choose a file to attach");
  expect(input).toHaveAttribute("accept", expect.stringContaining(".png"));
  fireEvent.change(input, { target: { files: [png()] } });
  await screen.findByRole("img", { name: "cat.png" });
  // The workflow stays selected after the upload.
  expect(screen.getByRole("button", { name: "Choose workflow" })).toHaveTextContent("Illustrate");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "make it blue" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));

  expect(onSend).toHaveBeenCalledWith("make it blue", "workflow-1", ["image-1"]);
});
