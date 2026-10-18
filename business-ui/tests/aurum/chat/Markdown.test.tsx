import { render, screen } from "@testing-library/react";
import { vi, describe, test, expect } from "vitest";
import { Markdown } from "@/components/aurum/chat/Markdown";

vi.mock("@/components/aurum/chat/MermaidDiagram", () => ({
  MermaidDiagram: ({ source }: { source: string }) => (
    <div data-testid="mermaid-diagram">{source}</div>
  ),
}));

describe("Markdown", () => {
  test("renders a plain fenced code block as a dark <pre><code> box, unaffected by mermaid support", () => {
    const { container } = render(
      <Markdown content={"```js\nconst x = 1;\n```"} />
    );

    const pre = container.querySelector("pre");
    expect(pre).toBeInTheDocument();
    expect(pre).toHaveClass("bg-[#1e1e1e]");
    expect(screen.queryByTestId("mermaid-diagram")).not.toBeInTheDocument();
    expect(container.textContent).toContain("const x = 1;");
  });

  test("renders inline code with the inline style, not the block one", () => {
    const { container } = render(<Markdown content={"Use `foo()` here."} />);

    const code = container.querySelector("code");
    expect(code).toBeInTheDocument();
    expect(code).toHaveClass("bg-aurum-surface-2");
    expect(container.querySelector("pre")).not.toBeInTheDocument();
  });

  test("routes a ```mermaid fenced block to MermaidDiagram instead of a code box", () => {
    const { container } = render(
      <Markdown content={"```mermaid\ngraph TD; A --> B;\n```"} />
    );

    expect(screen.getByTestId("mermaid-diagram")).toHaveTextContent(
      "graph TD; A --> B;"
    );
    expect(container.querySelector("pre.bg-\\[\\#1e1e1e\\]")).not.toBeInTheDocument();
  });
});
