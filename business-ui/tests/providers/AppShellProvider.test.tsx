import { render, screen, fireEvent } from "@testing-library/react";
import { vi } from "vitest";
import { AppShellProvider } from "@/providers/AppShellProvider";
import { ThemeProvider } from "@/providers/ThemeProvider";
import { useAppShell } from "@/hooks/useAppShell";

let authState = { isAuthenticated: false, isLoading: false };
let modelsData: { data: { id: string }[] } | undefined;
let settingsData:
  | {
      data: {
        default_model?: string | null;
        organization_default_model?: string | null;
      };
    }
  | undefined;
// Undefined settings are ambiguous — still fetching, or settled on an error.
// Tests that don't care get the "still fetching" reading, which is what
// having no data usually means.
let settingsPending: boolean | undefined;

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => authState,
}));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ data: modelsData }) }));
vi.mock("@/hooks/useSettings", () => ({
  useSettings: () => ({
    data: settingsData,
    isPending: settingsPending ?? settingsData === undefined,
  }),
}));

beforeEach(() => {
  authState = { isAuthenticated: false, isLoading: false };
  modelsData = undefined;
  settingsData = undefined;
  settingsPending = undefined;
});

function TestComponent() {
  const { sideMini, toggleSide, agents } = useAppShell();
  return (
    <div>
      <span data-testid="mini">{sideMini ? "yes" : "no"}</span>
      <span data-testid="agents">{agents.length}</span>
      <button onClick={toggleSide}>toggle side</button>
    </div>
  );
}

function ModelSpy() {
  const { models, activeModelId, setActiveModelId } = useAppShell();
  return (
    <div>
      <span data-testid="model-count">{models.length}</span>
      <span data-testid="active-model">{activeModelId}</span>
      <button onClick={() => setActiveModelId("manual")}>pick manual</button>
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

test("provides initial state and toggles sidebar", () => {
  render(
    <Wrapper>
      <TestComponent />
    </Wrapper>
  );
  expect(screen.getByTestId("agents").textContent).toBe("5");
  fireEvent.click(screen.getByText("toggle side"));
  expect(screen.getByTestId("mini").textContent).toBe("yes");
});

test("falls back to the mock model list when signed out", () => {
  render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );
  expect(screen.getByTestId("model-count").textContent).toBe("3");
  expect(screen.getByTestId("active-model").textContent).toBe("pro");
});

test("switches to the real model list and the backend's default_model once they load", () => {
  authState = { isAuthenticated: true, isLoading: false };
  modelsData = {
    data: [
      { id: "meta-llama/llama-3.1-8b-instruct:free" },
      { id: "openai/gpt-4o-mini" },
    ],
  };
  settingsData = { data: { default_model: "openai/gpt-4o-mini" } };

  render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  expect(screen.getByTestId("model-count").textContent).toBe("2");
  expect(screen.getByTestId("active-model").textContent).toBe("openai/gpt-4o-mini");
});

test("falls back to the first real model when default_model isn't in the list", () => {
  authState = { isAuthenticated: true, isLoading: false };
  modelsData = { data: [{ id: "model-a" }, { id: "model-b" }] };
  settingsData = { data: { default_model: "does-not-exist" } };

  render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  expect(screen.getByTestId("active-model").textContent).toBe("model-a");
});

test("uses the organization default when there is no valid personal model", () => {
  authState = { isAuthenticated: true, isLoading: false };
  modelsData = { data: [{ id: "model-a" }, { id: "model-b" }] };
  settingsData = {
    data: {
      default_model: "revoked-personal-model",
      organization_default_model: "model-b",
    },
  };

  render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  expect(screen.getByTestId("active-model").textContent).toBe("model-b");
});

test("keeps a valid personal model ahead of the organization default", () => {
  authState = { isAuthenticated: true, isLoading: false };
  modelsData = { data: [{ id: "model-a" }, { id: "model-b" }] };
  settingsData = {
    data: {
      default_model: "model-a",
      organization_default_model: "model-b",
    },
  };

  render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  expect(screen.getByTestId("active-model").textContent).toBe("model-a");
});

test("does not override a model the user picked manually", () => {
  authState = { isAuthenticated: true, isLoading: false };
  modelsData = { data: [{ id: "model-a" }, { id: "model-b" }] };
  settingsData = { data: { default_model: "model-a" } };

  const { rerender } = render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  fireEvent.click(screen.getByText("pick manual"));
  expect(screen.getByTestId("active-model").textContent).toBe("manual");

  modelsData = { data: [{ id: "model-c" }] };
  rerender(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  expect(screen.getByTestId("active-model").textContent).toBe("manual");
});

test("waits for settings when models load first, then applies the personal default", () => {
  authState = { isAuthenticated: true, isLoading: false };
  modelsData = { data: [{ id: "model-a" }, { id: "model-b" }] };
  const { rerender } = render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  settingsData = {
    data: {
      default_model: "model-b",
      organization_default_model: "model-a",
    },
  };
  rerender(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  expect(screen.getByTestId("active-model")).toHaveTextContent("model-b");
});

test("an authenticated empty catalog exposes no mock model or selection", () => {
  authState = { isAuthenticated: true, isLoading: false };
  modelsData = { data: [] };
  settingsData = { data: { default_model: "pro" } };

  render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  expect(screen.getByTestId("model-count")).toHaveTextContent("0");
  expect(screen.getByTestId("active-model")).toHaveTextContent("");
});

test("falls back to the first model when the settings request fails outright", () => {
  // /v1/settings/me can settle on an error — TanStack stops retrying and
  // `data` stays undefined forever. Gating on `data === undefined` left
  // activeModelId at "" for the rest of the session, so every send posted
  // `model: ""` and came back 403 from a populated model picker.
  authState = { isAuthenticated: true, isLoading: false };
  modelsData = { data: [{ id: "model-a" }, { id: "model-b" }] };
  settingsData = undefined;
  settingsPending = false;

  render(
    <Wrapper>
      <ModelSpy />
    </Wrapper>
  );

  expect(screen.getByTestId("active-model")).toHaveTextContent("model-a");
});

function ConversationSpy() {
  const { activeConversationId, setActiveConversationId } = useAppShell();
  return (
    <div>
      <span data-testid="active-conversation">{activeConversationId ?? "none"}</span>
      <button onClick={() => setActiveConversationId(null)}>new chat</button>
    </div>
  );
}

test("a new chat chosen before sign-in finishes is not replaced by the restored conversation", async () => {
  vi.useFakeTimers();
  try {
    const user = { orgId: "org-1", userId: "user-1" };
    const key = `aurum-ai.active-conversation:${user.orgId}:${user.userId}`;
    window.sessionStorage.setItem(key, "previous-conversation");
    authState = { isAuthenticated: false, isLoading: true } as typeof authState;
    const view = render(
      <ThemeProvider>
        <AppShellProvider>
          <ConversationSpy />
        </AppShellProvider>
      </ThemeProvider>
    );
    fireEvent.click(screen.getByText("new chat"));

    authState = { isAuthenticated: true, isLoading: false, user } as typeof authState;
    view.rerender(
      <ThemeProvider>
        <AppShellProvider>
          <ConversationSpy />
        </AppShellProvider>
      </ThemeProvider>
    );
    await vi.runAllTimersAsync();

    expect(screen.getByTestId("active-conversation")).toHaveTextContent("none");
    expect(window.sessionStorage.getItem(key)).toBeNull();
  } finally {
    vi.useRealTimers();
    window.sessionStorage.clear();
  }
});
