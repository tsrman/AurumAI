import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import * as React from "react";
import { vi } from "vitest";
import SettingsPage from "@/app/settings/page";
import type { UserSettings } from "@/hooks/useSettings";
import type { Model } from "@/lib/api";

let settingsResult: { data?: { data: UserSettings }; isLoading: boolean } = {
  data: undefined,
  isLoading: true,
};
let modelsResult: { data?: { data: Model[] }; isLoading: boolean } = {
  data: { data: [{ id: "gpt-4o-mini" }, { id: "gpt-4o" }] },
  isLoading: false,
};

const mutateAsync = vi.fn().mockResolvedValue(undefined);
let updateState = { mutateAsync, isPending: false, isSuccess: false, isError: false };
let authIsAuthenticated = true;

vi.mock("@/hooks/useSettings", () => ({
  useSettings: () => settingsResult,
  useUpdateSettings: () => updateState,
}));

vi.mock("@/components/aurum/settings/HermesMemorySection", () => ({
  HermesMemorySection: () => null,
}));
vi.mock("@/components/aurum/settings/HermesSkillDraftsSection", () => ({
  HermesSkillDraftsSection: () => null,
}));

vi.mock("@/hooks/useModels", () => ({
  useModels: () => modelsResult,
}));

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => ({ isAuthenticated: authIsAuthenticated }),
}));

const baseSettings: UserSettings = {
  user_id: "user-1",
  theme: "light",
  language: "ru",
  default_model: "gpt-4o",
  organization_default_model: "gpt-4o-mini",
  code_theme: "github",
  enter_to_send: false,
  show_timestamps: true,
  sidebar_collapsed: false,
  notifications: {},
  shortcuts: {},
};

beforeEach(() => {
  settingsResult = { data: { data: baseSettings }, isLoading: false };
  modelsResult = { data: { data: [{ id: "gpt-4o-mini" }, { id: "gpt-4o" }] }, isLoading: false };
  updateState = { mutateAsync, isPending: false, isSuccess: false, isError: false };
  authIsAuthenticated = true;
  mutateAsync.mockClear();
});

test("shows a loading state while settings or models are loading", () => {
  settingsResult = { data: undefined, isLoading: true };
  render(<SettingsPage />);
  expect(screen.getByText("Loading settings...")).toBeInTheDocument();
});

test("shows a loading state instead of an empty form before auth resolves", () => {
  authIsAuthenticated = false;
  // Disabled queries (auth not resolved yet) never fetch, so react-query's
  // own isLoading would be false here — the page must not rely on it alone.
  settingsResult = { data: undefined, isLoading: false };
  modelsResult = { data: undefined, isLoading: false };
  render(<SettingsPage />);
  expect(screen.getByText("Loading settings...")).toBeInTheDocument();
  expect(screen.queryByLabelText("Default model")).not.toBeInTheDocument();
});

test("pre-fills the form from the fetched settings and model list", () => {
  render(<SettingsPage />);
  expect(screen.getByLabelText("Default model")).toHaveValue("gpt-4o");
  expect(screen.getByLabelText("Show message times")).toBeChecked();
  expect(screen.getByLabelText("Send messages with Enter")).not.toBeChecked();
});

test("submits the edited form", async () => {
  render(<SettingsPage />);

  fireEvent.change(screen.getByLabelText("Default model"), {
    target: { value: "gpt-4o-mini" },
  });
  fireEvent.click(screen.getByLabelText("Send messages with Enter"));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
  expect(mutateAsync).toHaveBeenCalledWith({
    default_model: "gpt-4o-mini",
    enter_to_send: true,
  });
});

test("clears the personal model without copying the organization default", async () => {
  render(<SettingsPage />);

  fireEvent.change(screen.getByLabelText("Default model"), {
    target: { value: "" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
  expect(mutateAsync).toHaveBeenCalledWith({ default_model: null });
});

test("submits only changed fields and disables an unchanged form", async () => {
  render(<SettingsPage />);

  const save = screen.getByRole("button", { name: "Save" });
  expect(save).toBeDisabled();

  fireEvent.click(screen.getByLabelText("Send messages with Enter"));
  expect(save).toBeEnabled();
  fireEvent.click(save);

  await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
  expect(mutateAsync).toHaveBeenCalledWith({ enter_to_send: true });
});

test("shows a success message after saving", () => {
  updateState = { ...updateState, isSuccess: true };
  render(<SettingsPage />);
  expect(screen.getByText("Settings saved")).toBeInTheDocument();
});

test("shows an error message when saving fails", () => {
  updateState = { ...updateState, isError: true };
  render(<SettingsPage />);
  expect(screen.getByText("Could not save settings")).toBeInTheDocument();
});

test("keeps a stored model that is no longer accessible visible and clearable", async () => {
  // A grant can be revoked, or the model can leave the gateway, after the
  // preference was saved. With no matching <option> the browser fell back to
  // displaying the organization default while the stored value remains a
  // non-null model — and an untouched form leaves Save disabled, so there
  // was no way to clear it from this screen.
  settingsResult = {
    data: { data: { ...baseSettings, default_model: "revoked/model" } },
    isLoading: false,
  };
  render(<SettingsPage />);

  const select = screen.getByLabelText("Default model");
  expect(select).toHaveValue("revoked/model");
  expect(
    screen.getByRole("option", { name: "revoked/model (unavailable)" })
  ).toBeInTheDocument();

  fireEvent.change(select, { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));

  await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
  expect(mutateAsync).toHaveBeenCalledWith({ default_model: null });
});
