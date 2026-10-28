import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { HermesMemorySection } from "@/components/aurum/settings/HermesMemorySection";
import { useChatAgents } from "@/hooks/useChatAgents";
import { useClearHermesMemory, useHermesMemory } from "@/hooks/useHermesMemory";

vi.mock("@/hooks/useChatAgents", () => ({ useChatAgents: vi.fn() }));
vi.mock("@/hooks/useHermesMemory", () => ({ useHermesMemory: vi.fn(), useClearHermesMemory: vi.fn() }));

const mutate = vi.fn();

function given(memory: { memory: string; user: string } | undefined, hermes = true) {
  vi.mocked(useChatAgents).mockReturnValue({
    data: hermes ? [{ id: "hermes" }] : [],
  } as ReturnType<typeof useChatAgents>);
  vi.mocked(useHermesMemory).mockReturnValue({
    data: memory,
    isError: false,
  } as ReturnType<typeof useHermesMemory>);
  vi.mocked(useClearHermesMemory).mockReturnValue({
    mutate,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useClearHermesMemory>);
}

beforeEach(() => mutate.mockReset());

test("shows what Hermes remembers and clears it after confirming", () => {
  given({ memory: "The repo uses uv", user: "Prefers short answers" });
  render(<HermesMemorySection />);

  expect(screen.getByText("The repo uses uv")).toBeInTheDocument();
  expect(screen.getByText("Prefers short answers")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Clear memory" }));
  expect(mutate).not.toHaveBeenCalled();
  fireEvent.click(screen.getAllByRole("button", { name: "Clear memory" }).at(-1)!);
  expect(mutate).toHaveBeenCalledOnce();
});

test("says so when there is nothing to remember", () => {
  given({ memory: "", user: " " });
  render(<HermesMemorySection />);

  expect(screen.getByText("Hermes Agent remembers nothing yet.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Clear memory" })).not.toBeInTheDocument();
});

test("is not shown where Hermes cannot be used", () => {
  given(undefined, false);
  const { container } = render(<HermesMemorySection />);

  expect(container).toBeEmptyDOMElement();
});
