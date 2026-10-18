"""Deterministic embeddings for the standalone archive acceptance project only."""

import hashlib
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BYTES = 2 * 1024 * 1024


def embed(value: str) -> list[float]:
    digest = hashlib.shake_256(value.encode()).digest(1536 * 2)
    vector = [int.from_bytes(digest[i:i + 2], "big") / 32768 - 1 for i in range(0, len(digest), 2)]
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


def response_for(body: object) -> dict[str, object]:
    if not isinstance(body, dict):
        raise ValueError("invalid_batch")
    inputs = body.get("input")
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 64:
        raise ValueError("invalid_batch")
    if any(not isinstance(value, str) for value in inputs):
        raise ValueError("invalid_batch")
    return {"data": [{"index": index, "embedding": embed(value)} for index, value in enumerate(inputs)]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def reply(self, status: int, body: dict[str, object]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.reply(200 if self.path == "/health" else 404, {"fixture": True})

    def do_POST(self) -> None:
        if self.path != "/embeddings":
            self.reply(404, {"error": "unknown_path"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BYTES:
                raise ValueError("invalid_size")
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(length))
            response = response_for(body)
        except (ValueError, OSError):
            self.reply(400, {"error": "invalid_batch"})
            return
        self.reply(200, response)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 7997), Handler).serve_forever()
