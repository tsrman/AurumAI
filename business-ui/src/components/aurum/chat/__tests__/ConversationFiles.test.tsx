import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConversationFiles } from "../ConversationFiles";

const getBlob = vi.fn();

vi.mock("@/hooks/useAppShell", () => ({ useAppShell: () => ({ activeConversationId: "conv-1" }) }));
vi.mock("@/lib/files-api", () => ({
  getConversationFileBlob: (...args: unknown[]) => getBlob(...args),
}));

const files = [
  { fileId: "f-1", name: "report.csv", contentType: "text/csv", sizeBytes: 2048 },
  { fileId: "f-2", name: "summary.pdf", contentType: "application/pdf", sizeBytes: 3 * 1024 * 1024 },
];

describe("ConversationFiles", () => {
  afterEach(() => {
    getBlob.mockReset();
    vi.restoreAllMocks();
  });

  it("lists each file with its size and downloads it under its name", async () => {
    getBlob.mockResolvedValue(new Blob(["a,b\n"]));
    const created = vi.fn(() => "blob:report");
    Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() });
    const clicked: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(`${this.download}|${this.href}`);
    });

    render(<ConversationFiles files={files} />);

    const list = screen.getByRole("list", { name: "Attached files" });
    expect(list).toHaveTextContent("report.csv");
    expect(list).toHaveTextContent("2 kB");
    expect(list).toHaveTextContent("3 MB");
    fireEvent.click(screen.getByRole("button", { name: "Download report.csv" }));

    await waitFor(() => expect(clicked).toEqual(["report.csv|blob:report"]));
    expect(getBlob).toHaveBeenCalledWith("conv-1", "f-1");
  });

  it("says so when a download fails", async () => {
    getBlob.mockRejectedValue(new Error("404"));

    render(<ConversationFiles files={files} />);
    fireEvent.click(screen.getByRole("button", { name: "Download summary.pdf" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The file could not be downloaded.");
  });

  it("renders nothing without files", () => {
    const { container } = render(<ConversationFiles files={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
