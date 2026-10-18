export interface SSEMessage {
  event: string;
  data: string;
}

export async function* parseSSEStream(
  response: Response
): AsyncGenerator<SSEMessage> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Response body is not readable");

  const decoder = new TextDecoder();
  let buffer = "";
  let event = "";
  let data = "";

  const flush = (): SSEMessage | null => {
    if (event || data) {
      const message: SSEMessage = { event, data };
      event = "";
      data = "";
      return message;
    }
    return null;
  };

  const processLine = (line: string): SSEMessage | null => {
    // Event streams commonly use CRLF. `split("\n")` leaves the CR on the
    // empty delimiter line, which used to prevent the event from flushing.
    line = line.endsWith("\r") ? line.slice(0, -1) : line;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      const value = line.slice(5).trimStart();
      data = data ? `${data}\n${value}` : value;
    } else if (line === "") {
      return flush();
    }
    return null;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (value) {
        buffer += decoder.decode(value, { stream: true });
      }
      const lines = buffer.split("\n");
      buffer = done ? "" : (lines.pop() ?? "");

      for (const line of lines) {
        const message = processLine(line);
        if (message) yield message;
      }

      if (done) {
        const message = flush();
        if (message) yield message;
        break;
      }
    }
  } finally {
    reader.releaseLock();
  }
}
