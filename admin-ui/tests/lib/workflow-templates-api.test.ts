import { vi } from "vitest";

import {
  archiveWorkflowTemplate,
  captureWorkflowTemplate,
  getWorkflowTemplate,
  getWorkflowTemplates,
  instantiateWorkflowTemplate,
  replaceWorkflowTemplate,
} from "@/lib/workflow-templates-api";
import type {
  TemplateCaptureRequest,
  TemplateInstantiateRequest,
  TemplateReplaceRequest,
} from "@/lib/workflow-templates-api";

const capturePayload: TemplateCaptureRequest = {
  sourceWorkflowId: "workflow-1",
  sourceWorkflowVersion: 4,
  name: "Review template",
  description: "Reusable review",
};

const replacePayload: TemplateReplaceRequest = {
  ...capturePayload,
  version: 7,
};

const instantiatePayload: TemplateInstantiateRequest = {
  version: 7,
  name: "Review copy",
};

beforeEach(() => {
  vi.stubGlobal("window", {
    keycloak: {
      token: "admin-audience-token",
      updateToken: vi.fn(),
    },
  });
});

afterEach(() => vi.unstubAllGlobals());

test("template API sends the documented methods, paths, bearer audience, and camelCase bodies", async () => {
  const requests: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      requests.push(request.clone());
      return new Response(JSON.stringify({ data: {}, meta: {} }), {
        headers: { "Content-Type": "application/json" },
      });
    })
  );

  await getWorkflowTemplates(true);
  await getWorkflowTemplate("template-1");
  await captureWorkflowTemplate(capturePayload);
  await replaceWorkflowTemplate("template-1", replacePayload);
  await instantiateWorkflowTemplate("template-1", instantiatePayload);
  await archiveWorkflowTemplate("template-1", 7);

  expect(
    requests.map((request) => [
      request.method,
      new URL(request.url).pathname,
      request.headers.get("Authorization"),
    ])
  ).toEqual([
    ["GET", "/v1/admin/workflow-templates", "Bearer admin-audience-token"],
    ["GET", "/v1/admin/workflow-templates/template-1", "Bearer admin-audience-token"],
    ["POST", "/v1/admin/workflow-templates", "Bearer admin-audience-token"],
    ["PUT", "/v1/admin/workflow-templates/template-1", "Bearer admin-audience-token"],
    ["POST", "/v1/admin/workflow-templates/template-1/instantiate", "Bearer admin-audience-token"],
    ["POST", "/v1/admin/workflow-templates/template-1/archive", "Bearer admin-audience-token"],
  ]);
  expect(new URL(requests[0].url).searchParams.get("includeArchived")).toBe("true");
  await expect(requests[2].json()).resolves.toEqual(capturePayload);
  await expect(requests[3].json()).resolves.toEqual(replacePayload);
  await expect(requests[4].json()).resolves.toEqual(instantiatePayload);
  await expect(requests[5].json()).resolves.toEqual({ version: 7 });
});

test.each([
  ["capture", () => captureWorkflowTemplate(capturePayload)],
  ["replace", () => replaceWorkflowTemplate("template-1", replacePayload)],
  ["instantiate", () => instantiateWorkflowTemplate("template-1", instantiatePayload)],
  ["archive", () => archiveWorkflowTemplate("template-1", 7)],
])("%s mutation makes one request after a retryable response", async (_name, mutate) => {
  const fetchMock = vi.fn().mockResolvedValue(new Response("unavailable", { status: 503 }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(mutate()).rejects.toThrow();

  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test.each([
  ["409 response", () => new Response("conflict", { status: 409 })],
  ["network loss", () => Promise.reject(new TypeError("network lost"))],
])("template mutation makes one request after %s", async (_name, response) => {
  const fetchMock = vi.fn().mockImplementation(response);
  vi.stubGlobal("fetch", fetchMock);

  await expect(
    instantiateWorkflowTemplate("template-1", instantiatePayload)
  ).rejects.toThrow();

  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("template PUT does not retry a 503 response", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response("unavailable", { status: 503 }));
  vi.stubGlobal("fetch", fetchMock);

  await expect(
    replaceWorkflowTemplate("template-1", replacePayload)
  ).rejects.toThrow();

  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("template mutation does not refresh and replay an authentication failure", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response("unauthorized", { status: 401 }));
  const updateToken = vi.fn().mockImplementation(async () => {
    window.keycloak!.token = "refreshed-token";
    return true;
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("window", {
    keycloak: { token: "expired-token", updateToken },
  });

  await expect(captureWorkflowTemplate(capturePayload)).rejects.toThrow();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(updateToken).not.toHaveBeenCalled();
});
