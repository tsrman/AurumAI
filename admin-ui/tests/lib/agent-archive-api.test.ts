import { vi } from "vitest";
import { createTranslator } from "next-intl";

import { archiveAgentDefinition, deleteAgentDefinition, getAgentDefinitions, unarchiveAgentDefinition } from "@/lib/api";
import { getApiErrorMessage } from "@/lib/errors";

type Translator = Parameters<typeof getApiErrorMessage>[1];
import en from "../../messages/en.json";
import ru from "../../messages/ru.json";

beforeEach(() => {
  vi.stubGlobal("window", { keycloak: { token: "admin-token" } });
});

afterEach(() => vi.unstubAllGlobals());

function respond(status: number, body?: unknown) {
  const requests: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      requests.push(request.clone());
      return new Response(body === undefined ? null : JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return requests;
}

test("archiving, restoring and listing archived agents use the archive endpoints", async () => {
  const requests = respond(200, { data: [], meta: {} });

  await archiveAgentDefinition("agent-1");
  await unarchiveAgentDefinition("agent-1");
  await getAgentDefinitions(true);
  await getAgentDefinitions();

  expect(requests.map((r) => `${r.method} ${new URL(r.url).pathname}${new URL(r.url).search}`)).toEqual([
    "POST /v1/agent-definitions/agent-1/archive",
    "POST /v1/agent-definitions/agent-1/unarchive",
    "GET /v1/agent-definitions?includeArchived=true",
    "GET /v1/agent-definitions?includeArchived=false",
  ]);
});

test.each([
  ["agent_in_use", "active workflows"],
  ["agent_has_history", "cannot be deleted"],
  ["agent_in_active_run", "running workflow"],
  ["agent_name_archived", "archived agent already has this name"],
])("a %s conflict is explained, naming the workflows", async (code, text) => {
  respond(409, {
    error: { code: "conflict", message: { code, workflows: [{ id: "w1", name: "Hermes flow" }, { id: "w2", name: "Daily" }] } },
  });

  const error = await deleteAgentDefinition("agent-1").catch((err: unknown) => err);

  const message = getApiErrorMessage(error, createTranslator({ locale: "en", messages: en, namespace: "errors" }) as unknown as Translator);
  expect(message).toContain(text);
  expect(message).not.toContain("409");
  expect(message).toContain("Hermes flow, Daily");
});

test("the reasons exist in the Russian catalog too", async () => {
  respond(409, { error: { code: "conflict", message: { code: "agent_has_history", workflows: [] } } });
  const error = await deleteAgentDefinition("agent-1").catch((err: unknown) => err);
  const message = getApiErrorMessage(error, createTranslator({ locale: "ru", messages: ru, namespace: "errors" }) as unknown as Translator);
  expect(message).toBe(ru.errors.agent.agent_has_history);
});

test("a plain string conflict keeps its text", async () => {
  respond(409, { error: { code: "conflict", message: "Agent definition name already exists" } });
  const error = await deleteAgentDefinition("agent-1").catch((err: unknown) => err);
  expect((error as Error).message).toBe("Agent definition name already exists");
});
