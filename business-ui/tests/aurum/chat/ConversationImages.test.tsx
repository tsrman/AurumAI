import { render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { ConversationImages } from "@/components/aurum/chat/ConversationImages";

const getBlob = vi.hoisted(() => vi.fn());
let activeConversationId: string | null = "conv-1";

vi.mock("@/lib/images-api", () => ({ getConversationImageBlob: getBlob }));
vi.mock("@/hooks/useAppShell", () => ({ useAppShell: () => ({ activeConversationId }) }));

const A = "55555555-5555-4555-8555-555555555555";
const B = "66666666-6666-4666-8666-666666666666";

beforeEach(() => {
  activeConversationId = "conv-1";
  getBlob.mockReset();
  URL.createObjectURL = vi.fn(() => "blob:image");
  URL.revokeObjectURL = vi.fn();
});

test("loads each image through the authenticated API and offers open and download", async () => {
  getBlob.mockResolvedValue(new Blob(["png"], { type: "image/png" }));
  render(<ConversationImages imageIds={[A, B]} />);
  expect(screen.getAllByRole("status", { name: "Loading image…" })).toHaveLength(2);
  expect(await screen.findByAltText("Generated image 1")).toHaveAttribute("src", "blob:image");
  expect(screen.getByAltText("Generated image 2")).toBeInTheDocument();
  expect(getBlob).toHaveBeenCalledWith("conv-1", A);
  expect(screen.getByRole("link", { name: "Download image 1" })).toHaveAttribute("download", `image-${A}`);
});

test("shows a placeholder when an image cannot be loaded", async () => {
  getBlob.mockRejectedValue(new Error("404"));
  render(<ConversationImages imageIds={[A]} />);
  await waitFor(() => expect(screen.getByText("The image is unavailable.")).toBeInTheDocument());
});

test("renders nothing without an active conversation", () => {
  activeConversationId = null;
  const { container } = render(<ConversationImages imageIds={[A]} />);
  expect(container).toBeEmptyDOMElement();
  expect(getBlob).not.toHaveBeenCalled();
});

test("the user's attached images sit above their message", async () => {
  const { UserBubble } = await import("@/components/aurum/chat/UserBubble");
  getBlob.mockResolvedValue(new Blob(["png"], { type: "image/png" }));
  render(<UserBubble message={{ id: "u1", role: "user", content: "what is this", images: [A] }} />);
  expect(screen.getByRole("group", { name: "Attached images" })).toBeInTheDocument();
  expect(await screen.findByAltText("Attached image 1")).toHaveAttribute("src", "blob:image");
  expect(screen.getByText("what is this")).toBeInTheDocument();
});

test("an image-only message shows no empty bubble", async () => {
  const { UserBubble } = await import("@/components/aurum/chat/UserBubble");
  getBlob.mockResolvedValue(new Blob(["png"], { type: "image/png" }));
  const { container } = render(
    <UserBubble message={{ id: "u1", role: "user", content: "", images: [A] }} />
  );
  await screen.findByAltText("Attached image 1");
  expect(container.querySelector("p")).toBeNull();
});
