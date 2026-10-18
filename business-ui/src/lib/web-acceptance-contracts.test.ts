import { describe, expect, it } from "vitest";
import { initialTurnFailurePhase, verifyInitialTurnStream, verifyReconnect, verifyLiveRevocation } from "../../e2e/web-tools-assertions";

const turnId = "00000001-1111-4111-8111-111111111111";
function response(status = 200, mime = "text/event-stream", body = `event: turn-status\ndata: {"turnId":"${turnId}","status":"completed"}\n\n`) {
  return { status: () => status, headers: () => ({ "content-type": mime }), text: async () => body };
}

describe("web acceptance reconnect evidence", () => {
  it("requires actual terminal SSE and current-view final/source rendering", async () => {
    let rendered = false;
    await verifyReconnect(response(), turnId, async () => { rendered = true; return true; });
    expect(rendered).toBe(true);
    await expect(verifyReconnect(response(), turnId, async () => false)).rejects.toThrow("reconnect rendering");
  });
  it.each([
    response(503), response(200, "text/plain"), response(200, "text/event-stream", ""),
    response(200, "text/event-stream", 'event: turn-status\ndata: {"turnId":"other","status":"completed"}\n\n'),
  ])("cannot replace a broken SSE with a successful history reload", async (broken) => {
    let rendered = false;
    await expect(verifyReconnect(broken, turnId, async () => { rendered = true; return true; })).rejects.toThrow("reconnect stream");
    expect(rendered).toBe(false);
  });
});

describe("initial web turn stream evidence", () => {
  const expected = "WEB_E2E_FINAL";
  const turnResponse = (status: number, body: string) => ({
    status: () => status,
    text: async () => body,
  });

  it("requires the expected message-end and a completed stream before checking rendered output", async () => {
    const complete = `event: message-end\ndata: {"content":"${expected} [source:id]"}\n\nevent: done\ndata: {}\n\n`;
    await expect(verifyInitialTurnStream(turnResponse(200, complete), expected)).resolves.toBeUndefined();
    await expect(verifyInitialTurnStream(turnResponse(200, "event: done\ndata: {}\n\n"), expected)).rejects.toThrow("turn stream message-end absent");
    await expect(verifyInitialTurnStream(turnResponse(200, "event: message-end\ndata: {\"content\":\"unexpected\"}\n\nevent: done\ndata: {}\n\n"), expected)).rejects.toThrow("turn stream expected marker absent");
    await expect(verifyInitialTurnStream(turnResponse(200, "event: message-end\ndata: {\"content\":\"WEB_E2E_EMPTY_SEARCH\"}\n\nevent: done\ndata: {}\n\n"), expected)).rejects.toThrow("turn stream outcome empty-search");
    await expect(verifyInitialTurnStream(turnResponse(200, "event: message-end\ndata: {\"content\":\"WEB_E2E_ERROR:unsupported_document\"}\n\nevent: done\ndata: {}\n\n"), expected)).rejects.toThrow("turn stream outcome unsupported-document");
    await expect(verifyInitialTurnStream(turnResponse(200, "event: message-end\ndata: {\"content\":\"WEB_E2E_FINAL\"}\n\nevent: done\ndata: {}\n\n"), "WEB_E2E_ERROR:unsupported_document")).rejects.toThrow("turn stream outcome final");
    await expect(verifyInitialTurnStream(turnResponse(200, "event: error\ndata: {}\n\n"), expected)).rejects.toThrow("turn stream error");
    await expect(verifyInitialTurnStream(turnResponse(200, "event: progress\ndata: {}\n\n"), expected)).rejects.toThrow("turn stream terminal event absent");
  });

  it("never includes stream content in diagnostic errors", async () => {
    const privateContent = "private-stream-canary";
    const failure = verifyInitialTurnStream(turnResponse(200, privateContent), expected);
    await expect(failure).rejects.not.toThrow(privateContent);
  });

  it("maps only known SSE evidence failures to safe diagnostic phases", () => {
    expect(initialTurnFailurePhase(new Error("turn stream message-end absent"))).toBe("sources-turn-stream-message-end");
    expect(initialTurnFailurePhase(new Error("turn stream expected marker absent"))).toBe("sources-turn-stream-marker");
    expect(initialTurnFailurePhase(new Error("turn stream outcome empty-search"))).toBe("sources-turn-stream-empty-search");
    expect(initialTurnFailurePhase(new Error("turn stream outcome web-unavailable"))).toBe("sources-turn-stream-web-unavailable");
    expect(initialTurnFailurePhase(new Error("private-stream-canary"))).toBe("sources-turn-stream");
  });
});

describe("live revoke acceptance ordering", () => {
  it("waits for admitted turn before revoking, then separately checks a new hidden-tool turn", async () => {
    const events: string[] = [];
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    await verifyLiveRevocation({
      arm: async () => { events.push("arm"); },
      send: async () => { events.push("send"); await pending; events.push("denied"); },
      reached: async () => { events.push("admitted"); },
      revoke: async () => { events.push("revoke"); },
      release: async () => { events.push("release"); release(); },
      hidden: async () => { events.push("new-turn-hidden"); },
      grant: async () => { events.push("restore"); },
    });
    expect(events.slice(0, 7)).toEqual(["arm", "send", "admitted", "revoke", "release", "denied", "new-turn-hidden"]);
    expect(events.at(-1)).toBe("restore");
  });
  it("still restores permissions when release fails", async () => {
    let restored = false;
    await expect(verifyLiveRevocation({ arm: async () => {}, send: async () => {},
      reached: async () => {}, revoke: async () => {}, release: async () => { throw new Error("release failed"); },
      hidden: async () => {}, grant: async () => { restored = true; },
    })).rejects.toThrow("release failed");
    expect(restored).toBe(true);
  });
});
