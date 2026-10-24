import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";

import AdminModelsPage from "@/app/admin/models/page";
import type { AdminModelSettings } from "@/lib/api";

let access = {
  allowed: true,
  canWrite: true,
  error: null as string | null,
  isLoading: false,
};
let query: {
  data?: { data: AdminModelSettings };
  isLoading: boolean;
  error: unknown;
} = { data: undefined, isLoading: false, error: null };
const mutate = vi.fn(
  (
    _value: { defaultModel: string | null; defaultTemperature: number | null },
    options?: { onSuccess?: () => void }
  ) => options?.onSuccess?.()
);

vi.mock("@/hooks/useAdminAccess", () => ({
  useAdminAccess: () => access,
}));
vi.mock("@/components/authorization/GrantEditor", () => ({
  GrantEditor: () => null,
}));
let imageQuery: {
  data?: {
    data: {
      enabled: boolean;
      gatewayStatus: "available";
      generationModel: string | null;
      editModel: string | null;
      models: string[];
    };
  };
  isLoading: boolean;
  error: unknown;
} = { data: undefined, isLoading: false, error: null };
const imageMutate = vi.fn();

vi.mock("@/hooks/useAdminModels", () => ({
  useOrganizationImageModels: () => imageQuery,
  useUpdateOrganizationImageModels: () => ({
    mutate: imageMutate,
    isPending: false,
    isError: false,
    error: null,
  }),
  useAdminModels: () => query,
  useUpdateOrganizationModelSettings: () => ({
    mutate,
    isPending: false,
    isError: false,
    error: null,
  }),
}));

const available: AdminModelSettings = {
  gatewayStatus: "available",
  defaultModel: "provider/readable",
  defaultTemperature: null,
  defaultAvailable: true,
  models: [
    { id: "provider/readable", organizationReadable: true, isDefault: true },
    { id: "provider/private", organizationReadable: false, isDefault: false },
  ],
};

beforeEach(() => {
  access = { allowed: true, canWrite: true, error: null, isLoading: false };
  query = { data: { data: available }, isLoading: false, error: null };
  mutate.mockClear();
  imageMutate.mockClear();
  imageQuery = {
    data: {
      data: {
        enabled: true,
        gatewayStatus: "available",
        generationModel: null,
        editModel: null,
        models: ["provider/readable", "provider/image"],
      },
    },
    isLoading: false,
    error: null,
  };
});

test("shows access loading, denial, and query errors", () => {
  access = { allowed: false, canWrite: false, error: null, isLoading: true };
  const { rerender } = render(<AdminModelsPage />);
  expect(screen.getByText("Checking access...")).toBeInTheDocument();

  access = {
    allowed: false,
    canWrite: false,
    error: "Administrators only",
    isLoading: false,
  };
  rerender(<AdminModelsPage />);
  expect(screen.getByText("Administrators only")).toBeInTheDocument();

  access = { allowed: true, canWrite: true, error: null, isLoading: false };
  query = { data: undefined, isLoading: false, error: new Error("gateway broke") };
  rerender(<AdminModelsPage />);
  expect(screen.getByText("gateway broke")).toBeInTheDocument();
});

test("renders catalog and saves a cleared default", () => {
  render(<AdminModelsPage />);

  expect(screen.getByText("Gateway available")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  const catalog = screen.getByRole("table", { name: "Catalog" });
  expect(within(catalog).getByText("provider/private").closest("tr")).toHaveTextContent("Restricted");
  fireEvent.click(screen.getByRole("button", { name: "Access to provider/private" }));
  expect(screen.getByRole("dialog", { name: "Access to “provider/private”" })).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText("Default model"), {
    target: { value: "" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(mutate).toHaveBeenCalledWith(
    { defaultModel: null, defaultTemperature: null },
    expect.anything()
  );
});

test("allows choosing an ungranted model and explains access before saving", () => {
  const { rerender } = render(<AdminModelsPage />);
  const picker = screen.getByLabelText("Default model");
  const option = picker.querySelector('option[value="provider/private"]');
  expect(option).not.toBeDisabled();
  fireEvent.change(picker, { target: { value: "provider/private" } });

  expect(picker).toHaveValue("provider/private");
  expect(screen.getByText("Give the organization access to this model, then save the default. Choosing a model does not grant access."))
    .toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Give the organization access" }));
  expect(screen.getByRole("dialog", { name: "Access to “provider/private”" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  expect(mutate).not.toHaveBeenCalled();

  query = { data: { data: { ...available, models: [
    available.models[0], { ...available.models[1], organizationReadable: true },
  ] } }, isLoading: false, error: null };
  rerender(<AdminModelsPage />);
  expect(screen.getByLabelText("Default model")).toHaveValue("provider/private");
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(mutate).toHaveBeenCalledWith(
    { defaultModel: "provider/private", defaultTemperature: null }, expect.anything()
  );
});

test("shows stale defaults and mock gateway status", () => {
  query = {
    data: {
      data: {
        gatewayStatus: "mock",
        defaultModel: "removed/model",
        defaultTemperature: null,
        defaultAvailable: false,
        models: [],
      },
    },
    isLoading: false,
    error: null,
  };
  render(<AdminModelsPage />);

  expect(screen.getByText("Test catalog")).toBeInTheDocument();
  expect(
    screen.getByRole("option", { name: "removed/model — unavailable" })
  ).toBeDisabled();
  expect(screen.getByLabelText("Default model")).toHaveValue(
    "removed/model"
  );
});

test("during an outage only clearing the saved default remains available", () => {
  query = {
    data: {
      data: {
        gatewayStatus: "unavailable",
        defaultModel: "provider/readable",
        defaultTemperature: null,
        defaultAvailable: false,
        models: [],
      },
    },
    isLoading: false,
    error: null,
  };
  render(<AdminModelsPage />);

  expect(screen.getByText("Gateway catalog unavailable")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Default model"), {
    target: { value: "" },
  });
  expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
});

test("labels a catalogued default whose organization grant was revoked", () => {
  query = {
    data: {
      data: {
        gatewayStatus: "available",
        defaultModel: "provider/private",
        defaultTemperature: null,
        defaultAvailable: false,
        models: [
          {
            id: "provider/private",
            organizationReadable: false,
            isDefault: true,
          },
        ],
      },
    },
    isLoading: false,
    error: null,
  };

  render(<AdminModelsPage />);

  expect(
    screen.getByText("The saved model is no longer available to the organization.")
  ).toBeInTheDocument();
});


test("image generation and editing models are chosen separately", () => {
  render(<AdminModelsPage />);
  const images = screen.getByRole("heading", { name: "Images" }).closest("section")!;
  const save = within(images).getByRole("button", { name: "Save image models" });
  expect(save).toBeDisabled();
  fireEvent.change(within(images).getByLabelText("Image generation model"), {
    target: { value: "provider/image" },
  });
  fireEvent.click(save);
  expect(imageMutate).toHaveBeenCalledWith(
    { generationModel: "provider/image", editModel: null },
    expect.anything()
  );
});

test("explains when the operator switch keeps image tools off", () => {
  imageQuery = {
    ...imageQuery,
    data: { data: { ...imageQuery.data!.data, enabled: false, generationModel: "gone/model" } },
  };
  render(<AdminModelsPage />);
  expect(screen.getByText(/switched off on this deployment/)).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "gone/model — no longer published" })).toBeDisabled();
});
