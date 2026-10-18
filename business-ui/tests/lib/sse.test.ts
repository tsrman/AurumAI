import { parseSSEStream } from "@/lib/sse";

function streamResponse(body: string): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(body));
        controller.close();
      },
    })
  );
}

test("parses SSE event/data pairs", async () => {
  const response = streamResponse(
    "event: content-delta\ndata: {\"delta\":\"hello\"}\n\nevent: done\ndata: {}\n\n"
  );
  const messages: { event: string; data: string }[] = [];
  for await (const msg of parseSSEStream(response)) {
    messages.push(msg);
  }
  expect(messages).toHaveLength(2);
  expect(messages[0]).toEqual({ event: "content-delta", data: '{"delta":"hello"}' });
  expect(messages[1]).toEqual({ event: "done", data: "{}" });
});

test("parses trailing event without terminating blank line", async () => {
  const response = streamResponse(
    "event: content-delta\ndata: {\"delta\":\"hello\"}\n"
  );
  const messages: { event: string; data: string }[] = [];
  for await (const msg of parseSSEStream(response)) {
    messages.push(msg);
  }
  expect(messages).toHaveLength(1);
  expect(messages[0]).toEqual({ event: "content-delta", data: '{"delta":"hello"}' });
});

test("concatenates multi-line data fields with newline", async () => {
  const response = streamResponse(
    "event: message\ndata: line1\ndata: line2\ndata: line3\n\n"
  );
  const messages: { event: string; data: string }[] = [];
  for await (const msg of parseSSEStream(response)) {
    messages.push(msg);
  }
  expect(messages).toHaveLength(1);
  expect(messages[0]).toEqual({ event: "message", data: "line1\nline2\nline3" });
});
