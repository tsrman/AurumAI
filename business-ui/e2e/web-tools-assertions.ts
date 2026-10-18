type ReconnectResponse = {
  status(): number;
  headers(): Record<string, string>;
  text(): Promise<string>;
};

type InitialTurnResponse = {
  status(): number;
  text(): Promise<string>;
};

export function initialTurnFailurePhase(error: unknown): string {
  if (error instanceof Error && error.message === "turn stream message-end absent") {
    return "sources-turn-stream-message-end";
  }
  if (error instanceof Error && error.message === "turn stream expected marker absent") {
    return "sources-turn-stream-marker";
  }
  const safeOutcomes: Record<string, string> = {
    "turn stream outcome empty-search": "sources-turn-stream-empty-search",
    "turn stream outcome invalid-tool-data": "sources-turn-stream-invalid-tool-data",
    "turn stream outcome web-unavailable": "sources-turn-stream-web-unavailable",
    "turn stream outcome web-timeout": "sources-turn-stream-web-timeout",
    "turn stream outcome fixture-not-selected": "sources-turn-stream-fixture-not-selected",
  };
  if (error instanceof Error && safeOutcomes[error.message]) {
    return safeOutcomes[error.message];
  }
  return "sources-turn-stream";
}

export async function verifyInitialTurnStream(
  response: InitialTurnResponse,
  expectedMarker: string,
): Promise<void> {
  if (response.status() !== 200) throw new Error("turn stream response rejected");
  let body: string;
  try {
    body = await response.text();
  } catch {
    throw new Error("turn stream body unavailable");
  }
  if (/^event: error\r?$/m.test(body)) throw new Error("turn stream error");
  if (!/^event: done\r?$/m.test(body)) throw new Error("turn stream terminal event absent");
  const messageEnds = body.split(/\r?\n\r?\n/).filter((frame) =>
    frame.split(/\r?\n/).some((line) => line === "event: message-end")
  );
  if (messageEnds.length === 0) throw new Error("turn stream message-end absent");
  const contents = messageEnds.flatMap((frame) => {
    const encoded = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    try {
      const payload = JSON.parse(encoded) as { content?: unknown };
      return typeof payload.content === "string" ? [payload.content] : [];
    } catch {
      return [];
    }
  });
  if (!contents.some((content) => content.includes(expectedMarker))) {
    const knownOutcome = contents.map((content) => {
      if (content === "WEB_E2E_EMPTY_SEARCH") return "empty-search";
      if (content === "WEB_E2E_INVALID_TOOL_DATA") return "invalid-tool-data";
      if (content === "Hello from the deterministic Kubernetes gateway.") return "fixture-not-selected";
      if (content === "WEB_E2E_ERROR:web_unavailable") return "web-unavailable";
      if (content === "WEB_E2E_ERROR:web_timeout") return "web-timeout";
      if (content === "WEB_E2E_ERROR:unsupported_document") return "unsupported-document";
      if (content.startsWith("WEB_E2E_FINAL")) return "final";
      return null;
    }).find((outcome) => outcome !== null);
    if (knownOutcome) throw new Error(`turn stream outcome ${knownOutcome}`);
    throw new Error("turn stream expected marker absent");
  }
  if (body.includes("IGNORE ALL PREVIOUS INSTRUCTIONS")) {
    throw new Error("turn stream contains unsafe page instructions");
  }
}

export async function verifyReconnect(response: ReconnectResponse, turnId: string, rendered: () => Promise<boolean>): Promise<void> {
  if (response.status() !== 200 || !response.headers()["content-type"]?.startsWith("text/event-stream")) {
    throw new Error("reconnect stream response rejected");
  }
  let completed = false;
  try {
    for (const frame of (await response.text()).split(/\r?\n\r?\n/)) {
      if (!/^event: turn-status\r?$/m.test(frame)) continue;
      const payload = /^data: (.+)\r?$/m.exec(frame)?.[1];
      const data = payload ? JSON.parse(payload) as { turnId?: string; status?: string } : {};
      if (data.turnId === turnId && data.status === "completed") completed = true;
    }
  } catch {
    throw new Error("reconnect stream body unavailable");
  }
  if (!completed) throw new Error("reconnect stream terminal event absent");
  if (!await rendered()) throw new Error("reconnect rendering absent in active conversation");
}

type RevocationOperations = Record<"arm" | "send" | "reached" | "revoke" | "release" | "hidden" | "grant", () => Promise<unknown>>;

export async function verifyLiveRevocation(operations: RevocationOperations): Promise<void> {
  await operations.arm();
  // Attach the rejection handler immediately while waiting for server admission.
  const pending = operations.send().then(() => false, () => true);
  try {
    await operations.reached();
    await operations.revoke();
    await operations.release();
    if (await pending) throw new Error("in-flight revoke result assertion failed");
    await operations.hidden();
  } finally {
    try {
      await operations.release();
    } finally {
      await operations.grant();
      await pending;
    }
  }
}
