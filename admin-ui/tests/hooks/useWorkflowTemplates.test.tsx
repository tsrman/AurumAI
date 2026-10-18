import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { vi } from "vitest";

import {
  useArchiveWorkflowTemplate,
  useCaptureWorkflowTemplate,
  useInstantiateWorkflowTemplate,
  useReplaceWorkflowTemplate,
  useWorkflowTemplate,
  useWorkflowTemplates,
  workflowTemplateKeys,
} from "@/hooks/useWorkflowTemplates";
import { workflowKeys } from "@/hooks/useWorkflows";
import type {
  TemplateCaptureRequest,
  TemplateInstantiateRequest,
  TemplateReplaceRequest,
} from "@/lib/workflow-templates-api";

const getWorkflowTemplates = vi.fn();
const getWorkflowTemplate = vi.fn();
const captureWorkflowTemplate = vi.fn();
const replaceWorkflowTemplate = vi.fn();
const instantiateWorkflowTemplate = vi.fn();
const archiveWorkflowTemplate = vi.fn();

let authState: {
  user: { orgId?: string; role: string } | null;
  isLoading: boolean;
} = { user: { orgId: "org-1", role: "org_admin" }, isLoading: false };

vi.mock("@/providers/AuthProvider", () => ({
  useAuth: () => authState,
}));

vi.mock("@/lib/workflow-templates-api", () => ({
  getWorkflowTemplates: (includeArchived: boolean) =>
    getWorkflowTemplates(includeArchived),
  getWorkflowTemplate: (id: string) => getWorkflowTemplate(id),
  captureWorkflowTemplate: (payload: TemplateCaptureRequest) =>
    captureWorkflowTemplate(payload),
  replaceWorkflowTemplate: (id: string, payload: TemplateReplaceRequest) =>
    replaceWorkflowTemplate(id, payload),
  instantiateWorkflowTemplate: (id: string, payload: TemplateInstantiateRequest) =>
    instantiateWorkflowTemplate(id, payload),
  archiveWorkflowTemplate: (id: string, version: number) =>
    archiveWorkflowTemplate(id, version),
}));

let client: QueryClient;

function Wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  authState = { user: { orgId: "org-1", role: "org_admin" }, isLoading: false };
  vi.clearAllMocks();
  client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: 3, retryDelay: 0 },
    },
  });
});

test("template queries are disabled while admin access is pending or denied", async () => {
  getWorkflowTemplates.mockResolvedValue({ data: [], meta: {} });
  authState = { user: null, isLoading: true };
  const { rerender } = renderHook(() => useWorkflowTemplates(false, true), {
    wrapper: Wrapper,
  });

  await act(async () => undefined);
  expect(getWorkflowTemplates).not.toHaveBeenCalled();

  authState = { user: { orgId: "org-1", role: "member" }, isLoading: false };
  rerender();
  await act(async () => undefined);
  expect(getWorkflowTemplates).not.toHaveBeenCalled();
});

test("query keys contain organization, archived filter, identity, and detail version", async () => {
  getWorkflowTemplates.mockResolvedValue({ data: [], meta: {} });
  getWorkflowTemplate.mockResolvedValue({ data: { id: "template-1" }, meta: {} });

  const { result } = renderHook(
    () => ({
      list: useWorkflowTemplates(true, true),
      detail: useWorkflowTemplate("template-1", 7, true),
    }),
    { wrapper: Wrapper }
  );

  await waitFor(() => {
    expect(result.current.list.isSuccess).toBe(true);
    expect(result.current.detail.isSuccess).toBe(true);
  });
  expect(client.getQueryState(workflowTemplateKeys.list("org-1", true))).toBeDefined();
  expect(
    client.getQueryState(workflowTemplateKeys.detail("org-1", "template-1", 7))
  ).toBeDefined();
});

test("switching organizations never exposes the previous organization's template data", async () => {
  let resolveOrg2!: (value: { data: string[]; meta: object }) => void;
  getWorkflowTemplates
    .mockResolvedValueOnce({ data: ["org-1-template"], meta: {} })
    .mockImplementationOnce(
      () => new Promise((resolve) => {
        resolveOrg2 = resolve;
      })
    );
  const { result, rerender } = renderHook(
    () => useWorkflowTemplates(false, true),
    { wrapper: Wrapper }
  );
  await waitFor(() => expect(result.current.data?.data).toEqual(["org-1-template"]));

  authState = { user: { orgId: "org-2", role: "org_admin" }, isLoading: false };
  rerender();

  expect(result.current.data).toBeUndefined();
  await act(async () => resolveOrg2({ data: ["org-2-template"], meta: {} }));
  await waitFor(() => expect(result.current.data?.data).toEqual(["org-2-template"]));
});

test("every mutation disables retry even when the QueryClient default enables it", async () => {
  const failure = new Error("failed");
  captureWorkflowTemplate.mockRejectedValue(failure);
  replaceWorkflowTemplate.mockRejectedValue(failure);
  instantiateWorkflowTemplate.mockRejectedValue(failure);
  archiveWorkflowTemplate.mockRejectedValue(failure);
  const capturePayload = {
    sourceWorkflowId: "workflow-1",
    sourceWorkflowVersion: 1,
    name: "Template",
  } satisfies TemplateCaptureRequest;
  const replacePayload = { ...capturePayload, version: 1 } satisfies TemplateReplaceRequest;
  const instantiatePayload = { version: 1, name: "Copy" } satisfies TemplateInstantiateRequest;

  const { result } = renderHook(
    () => ({
      capture: useCaptureWorkflowTemplate(),
      replace: useReplaceWorkflowTemplate(),
      instantiate: useInstantiateWorkflowTemplate(),
      archive: useArchiveWorkflowTemplate(),
    }),
    { wrapper: Wrapper }
  );

  await act(async () => {
    await Promise.allSettled([
      result.current.capture.mutateAsync(capturePayload),
      result.current.replace.mutateAsync({ id: "template-1", payload: replacePayload }),
      result.current.instantiate.mutateAsync({ id: "template-1", payload: instantiatePayload }),
      result.current.archive.mutateAsync({ id: "template-1", version: 1 }),
    ]);
  });

  expect(captureWorkflowTemplate).toHaveBeenCalledTimes(1);
  expect(replaceWorkflowTemplate).toHaveBeenCalledTimes(1);
  expect(instantiateWorkflowTemplate).toHaveBeenCalledTimes(1);
  expect(archiveWorkflowTemplate).toHaveBeenCalledTimes(1);
});

test("successful mutations invalidate templates and instantiation also refreshes workflows and agents", async () => {
  captureWorkflowTemplate.mockResolvedValue({ data: {}, meta: {} });
  replaceWorkflowTemplate.mockResolvedValue({ data: {}, meta: {} });
  instantiateWorkflowTemplate.mockResolvedValue({ data: {}, meta: {} });
  archiveWorkflowTemplate.mockResolvedValue({ data: {}, meta: {} });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const capturePayload = {
    sourceWorkflowId: "workflow-1",
    sourceWorkflowVersion: 1,
    name: "Template",
  } satisfies TemplateCaptureRequest;

  const { result } = renderHook(
    () => ({
      capture: useCaptureWorkflowTemplate(),
      replace: useReplaceWorkflowTemplate(),
      instantiate: useInstantiateWorkflowTemplate(),
      archive: useArchiveWorkflowTemplate(),
    }),
    { wrapper: Wrapper }
  );

  await act(async () => {
    await result.current.capture.mutateAsync(capturePayload);
    await result.current.replace.mutateAsync({
      id: "template-1",
      payload: { ...capturePayload, version: 1 },
    });
    await result.current.instantiate.mutateAsync({
      id: "template-1",
      payload: { version: 1, name: "Copy" },
    });
    await result.current.archive.mutateAsync({ id: "template-1", version: 1 });
  });

  expect(invalidate).toHaveBeenCalledTimes(6);
  expect(invalidate).toHaveBeenCalledWith({ queryKey: workflowTemplateKeys.all });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: workflowKeys.all });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-definitions"] });
});

test("a 409 invalidates stale template queries without resubmitting a new version", async () => {
  const conflict = Object.assign(new Error("conflict"), {
    response: new Response("conflict", { status: 409 }),
  });
  replaceWorkflowTemplate.mockRejectedValue(conflict);
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const payload = {
    sourceWorkflowId: "workflow-1",
    sourceWorkflowVersion: 1,
    name: "Template",
    version: 7,
  } satisfies TemplateReplaceRequest;
  const { result } = renderHook(() => useReplaceWorkflowTemplate(), {
    wrapper: Wrapper,
  });

  await act(async () => {
    await expect(
      result.current.mutateAsync({ id: "template-1", payload })
    ).rejects.toThrow("conflict");
  });

  expect(replaceWorkflowTemplate).toHaveBeenCalledTimes(1);
  expect(replaceWorkflowTemplate).toHaveBeenCalledWith("template-1", payload);
  expect(invalidate).toHaveBeenCalledWith({ queryKey: workflowTemplateKeys.all });
});
