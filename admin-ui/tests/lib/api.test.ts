import { vi } from "vitest";
import { HTTPError } from "ky";
import {
  api,
  getAdminModels,
  getAdminUsage,
  getAdminUsers,
  getAuthorizationSubjects,
  putOrganizationModelSettings,
  previewAuthorization,
  reconcileResourceGrants,
} from "@/lib/api";

function make502() {
  return new Response("bad gateway", { status: 502 });
}

function make401() {
  return new Response("unauthorized", { status: 401 });
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(make502()));
  vi.stubGlobal("window", { keycloak: { token: "test-token" } });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("does not retry a POST that fails with 502 (not idempotent)", async () => {
  await expect(api.post("skills", { json: { name: "x" } })).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
});

test("does not retry a PATCH that fails with 502 (not idempotent)", async () => {
  await expect(api.patch("skills/1", { json: { name: "x" } })).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
});

test("still retries a GET that fails with 502 (safe to repeat)", async () => {
  vi.useFakeTimers();
  const resultPromise = api.get("skills").catch(() => "rejected");

  // ky's retry backoff runs on real setTimeout internally; fast-forward
  // through all 3 retry delays without slowing the test down.
  for (let i = 0; i < 3; i++) {
    await vi.advanceTimersByTimeAsync(10_000);
  }

  await resultPromise;
  expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(1);
});

test("surfaces the original 401 when the refresh token itself is invalid", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(make401()));
  vi.stubGlobal("window", {
    keycloak: {
      token: "test-token",
      // Mirrors a real keycloak-js rejection when the refresh token is
      // expired/invalid — must not replace the original 401 HTTPError.
      updateToken: vi.fn().mockRejectedValue(new Error("invalid_grant")),
    },
  });

  let caught: unknown;
  try {
    await api.get("skills");
  } catch (err) {
    caught = err;
  }

  expect(caught).toBeInstanceOf(HTTPError);
  expect((caught as HTTPError).response.status).toBe(401);
});

test("ordinary GET callers still refresh and replay a 401 response", async () => {
  const requests: Request[] = [];
  const fetchMock = vi.fn().mockImplementation(async (request: Request) => {
    requests.push(request.clone());
    if (requests.length === 1) return make401();
    return new Response(JSON.stringify({ data: [], meta: {} }), {
      headers: { "Content-Type": "application/json" },
    });
  });
  const updateToken = vi.fn().mockImplementation(async () => {
    window.keycloak!.token = "refreshed-token";
    return true;
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("window", {
    keycloak: { token: "expired-token", updateToken },
  });

  await expect(api.get("skills").json()).resolves.toEqual({ data: [], meta: {} });

  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(updateToken).toHaveBeenCalledTimes(1);
  expect(requests[0].headers.get("Authorization")).toBe("Bearer expired-token");
  expect(requests[1].headers.get("Authorization")).toBe("Bearer refreshed-token");
});

test("authorization admin subject lookup uses its dedicated endpoint", async () => {
  const fetchMock = vi.fn().mockImplementation(async () =>
    new Response(JSON.stringify({ data: [], meta: {} }), {
      headers: { "Content-Type": "application/json" },
    })
  );
  vi.stubGlobal("fetch", fetchMock);

  await getAuthorizationSubjects();

  const paths = fetchMock.mock.calls.map(([request]) => new URL((request as Request).url).pathname);
  expect(paths).toEqual(["/v1/admin/authorization/subjects"]);
});

test("authorization writes send complete reconcile and preview payloads to typed endpoints", async () => {
  const requests: Request[] = [];
  const fetchMock = vi.fn().mockImplementation(async (request: Request) => {
    requests.push(request.clone());
    return (
    new Response(JSON.stringify({ data: [], meta: {} }), {
      headers: { "Content-Type": "application/json" },
    })
    );
  });
  vi.stubGlobal("fetch", fetchMock);

  await reconcileResourceGrants({
    resourceType: "skill",
    resourceId: "skill-1",
    grants: [{ principalType: "group", principalId: "group-1", permission: "read" }],
  });
  await previewAuthorization({
    subjectType: "user",
    subjectId: "user-1",
    resources: [{ resourceType: "skill", resourceId: "skill-1" }],
  });

  const [reconcileRequest, previewRequest] = requests;
  expect(new URL(reconcileRequest.url).pathname).toBe(
    "/v1/admin/authorization/grants/reconcile"
  );
  expect(reconcileRequest.method).toBe("PUT");
  await expect(reconcileRequest.clone().json()).resolves.toEqual({
    resourceType: "skill",
    resourceId: "skill-1",
    grants: [{ principalType: "group", principalId: "group-1", permission: "read" }],
  });
  expect(new URL(previewRequest.url).pathname).toBe(
    "/v1/admin/authorization/preview/users/user-1"
  );
  expect(previewRequest.method).toBe("POST");
  await expect(previewRequest.clone().json()).resolves.toEqual({
    resources: [{ resourceType: "skill", resourceId: "skill-1" }],
  });
});

test("organization model settings use dedicated typed admin endpoints", async () => {
  const requests: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      requests.push(request.clone());
      return new Response(
        JSON.stringify({ data: { defaultModel: null, models: [] }, meta: {} }),
        { headers: { "Content-Type": "application/json" } }
      );
    })
  );

  await getAdminModels();
  await putOrganizationModelSettings({
    defaultModel: "provider/model",
    defaultTemperature: 0.5,
  });

  expect(new URL(requests[0].url).pathname).toBe("/v1/admin/models");
  expect(new URL(requests[1].url).pathname).toBe(
    "/v1/admin/settings/models"
  );
  expect(requests[1].method).toBe("PUT");
  await expect(requests[1].json()).resolves.toEqual({
    defaultModel: "provider/model",
    defaultTemperature: 0.5,
  });
});

test("admin analytics and user directory serialize only explicit filters", async () => {
  const requests: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      requests.push(request.clone());
      return new Response(JSON.stringify({ data: [], meta: {} }), {
        headers: { "Content-Type": "application/json" },
      });
    })
  );

  await getAdminUsage({
    from: "2026-09-01",
    to: "2026-09-30",
    userId: "user-1",
    model: "model/a",
  });
  await getAdminUsers({
    limit: 50,
    offset: 0,
    search: "Ivan",
    status: "active",
  });
  await getAdminUsers({ limit: 25, offset: 50 });

  expect(Object.fromEntries(new URL(requests[0].url).searchParams)).toEqual({
    from: "2026-09-01",
    to: "2026-09-30",
    userId: "user-1",
    model: "model/a",
  });
  expect(new URL(requests[1].url).pathname).toBe("/v1/admin/users");
  expect(Object.fromEntries(new URL(requests[1].url).searchParams)).toEqual({
    limit: "50",
    offset: "0",
    search: "Ivan",
    status: "active",
  });
  expect(Object.fromEntries(new URL(requests[2].url).searchParams)).toEqual({
    limit: "25",
    offset: "50",
  });
});
