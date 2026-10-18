import { render, screen, waitFor } from "@testing-library/react";
import { vi, describe, test, expect, beforeEach } from "vitest";
import { MermaidDiagram } from "@/components/aurum/chat/MermaidDiagram";
import { ThemeProvider } from "@/providers/ThemeProvider";

const renderMock = vi.fn();
const initializeMock = vi.fn();

vi.mock("mermaid", () => ({
  default: {
    initialize: (...args: unknown[]) => initializeMock(...args),
    render: (...args: unknown[]) => renderMock(...args),
  },
}));

function renderDiagram(source: string) {
  return render(
    <ThemeProvider>
      <MermaidDiagram source={source} />
    </ThemeProvider>
  );
}

describe("MermaidDiagram", () => {
  beforeEach(() => {
    renderMock.mockReset();
    initializeMock.mockReset();
  });

  test("shows a loading state, then the rendered SVG", async () => {
    renderMock.mockResolvedValue({ svg: "<svg data-testid='diagram-svg'></svg>" });

    const { container } = renderDiagram("graph TD; A --> B;");

    expect(screen.getByText("Rendering diagram…")).toBeInTheDocument();

    await waitFor(() => {
      expect(container.querySelector("svg")).toBeInTheDocument();
    });
    expect(screen.queryByText("Rendering diagram…")).not.toBeInTheDocument();
    expect(renderMock).toHaveBeenCalledWith(expect.stringMatching(/^mermaid-/), "graph TD; A --> B;");
  });

  test("falls back to the raw source in a code block when mermaid.render rejects", async () => {
    renderMock.mockRejectedValue(new Error("Parse error on line 1"));

    renderDiagram("not a real diagram");

    await waitFor(() => {
      expect(screen.getByText(/Could not render the diagram/)).toBeInTheDocument();
    });
    expect(screen.getByText("not a real diagram")).toBeInTheDocument();
  });

  test("initializes mermaid with securityLevel strict", async () => {
    renderMock.mockResolvedValue({ svg: "<svg></svg>" });

    renderDiagram("graph TD; A --> B;");

    await waitFor(() => expect(initializeMock).toHaveBeenCalled());
    expect(initializeMock).toHaveBeenCalledWith(
      expect.objectContaining({ securityLevel: "strict" })
    );
  });
});
