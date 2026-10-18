import { render, screen, fireEvent } from "@testing-library/react";
import { vi } from "vitest";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { ThemeProvider } from "@/providers/ThemeProvider";
import { useAppShell } from "@/hooks/useAppShell";

let authState = { isAuthenticated: false, isLoading: false };
let modelsData: { data: { id: string }[] } | undefined;

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => authState,
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: modelsData }) }));

beforeEach(() => {
  authState = { isAuthenticated: false, isLoading: false };
  modelsData = undefined;
});

function ModelSpy() {
  const { models } = useAppShell();
  return <span data-testid="model-count">{models.length}</span>;
}

function ConstructorModelSpy() {
  const { constructorState, setConstructorState } = useAppShell();
  return (
    <div>
      <span data-testid="constructor-model">{constructorState.model}</span>
      <button
        onClick={() =>
          setConstructorState((prev) => ({ ...prev, model: "model-a" }))
        }
      >
        pick model-a
      </button>
    </div>
  );
}

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider>
      <AppShellProvider>{children}</AppShellProvider>
    </ThemeProvider>
  );
}

test("falls back to the mock model list when signed out", () => {
  render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );
  expect(screen.getByTestId("model-count").textContent).toBe("3");
});

test("switches to the real model list once it loads", () => {
  authState = { isAuthenticated: true, isLoading: false };
  modelsData = { data: [{ id: "model-a" }, { id: "model-b" }] };

  render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  expect(screen.getByTestId("model-count").textContent).toBe("2");
});

test("agent constructor seeds the mock model id by default", () => {
  render(
    <Wrapper>
      <ConstructorModelSpy />
    </Wrapper>
  );
  expect(screen.getByTestId("constructor-model").textContent).toBe("pro");
});

test("does not override a model already picked in the agent constructor", () => {
  authState = { isAuthenticated: true, isLoading: false };
  modelsData = { data: [{ id: "model-a" }, { id: "model-b" }] };
  render(
    <Wrapper>
      <ConstructorModelSpy />
    </Wrapper>
  );

  fireEvent.click(screen.getByText("pick model-a"));
  expect(screen.getByTestId("constructor-model").textContent).toBe("model-a");
});
