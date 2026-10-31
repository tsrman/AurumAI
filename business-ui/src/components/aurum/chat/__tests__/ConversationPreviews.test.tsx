import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConversationPreviews } from "../ConversationPreviews";

const ticket = vi.fn();

vi.mock("@/hooks/useAppShell", () => ({ useAppShell: () => ({ activeConversationId: "conv-1" }) }));
vi.mock("@/lib/previews-api", () => ({ previewTicketUrl: (...args: unknown[]) => ticket(...args) }));

const previews = [{ previewId: "p-1", port: 5173 }];

describe("ConversationPreviews", () => {
  afterEach(() => {
    ticket.mockReset();
    vi.restoreAllMocks();
  });

  it("opens the preview's ticket URL in a new tab without a handle on this page", async () => {
    ticket.mockResolvedValue("http://abc.preview.localhost:8095/__aurum/enter?ticket=t");
    const tab = { opener: window as unknown, location: { href: "" }, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);

    render(<ConversationPreviews previews={previews} />);
    expect(screen.getByRole("group", { name: "Running apps" })).toHaveTextContent("Port 5173");
    fireEvent.click(screen.getByRole("button", { name: "Open prototype" }));

    await waitFor(() => expect(tab.location.href).toBe("http://abc.preview.localhost:8095/__aurum/enter?ticket=t"));
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(tab.opener).toBeNull();
    expect(ticket).toHaveBeenCalledWith("conv-1", "p-1");
  });

  it("closes the tab and says so when the preview has stopped", async () => {
    ticket.mockRejectedValue(new Error("404"));
    const tab = { opener: null, location: { href: "" }, close: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);

    render(<ConversationPreviews previews={previews} />);
    fireEvent.click(screen.getByRole("button", { name: "Open prototype" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The prototype is no longer running.");
    expect(tab.close).toHaveBeenCalled();
  });
});
