"""Probes run inside a container on the Hermes sandbox network.

Standard library only (plus pip for `pip`). Each command prints one JSON line;
the token comes from HERMES_TOKEN and is never printed.
"""

import base64
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time

EGRESS = "hermes-egress"
TOKEN = os.environ.get("HERMES_TOKEN", "")


def emit(**fields: object) -> None:
    print(json.dumps(fields, sort_keys=True))


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def direct(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=3).close()
        return True
    except OSError:
        return False


def raw(port: int, data: bytes) -> bytes:
    with socket.create_connection((EGRESS, port), timeout=10) as sock:
        sock.sendall(data)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
        return b"".join(chunks)


def completion_request(target: str = "/v1/hermes/chat/completions", stream: bool = True) -> bytes:
    body = json.dumps(
        {"model": "hermes-egress-test", "messages": [{"role": "user", "content": "hello"}], "stream": stream}
    ).encode()
    return (
        f"POST {target} HTTP/1.1\r\nHost: {EGRESS}\r\nAuthorization: Bearer {TOKEN}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body


def connect_result(target: str, token: str = TOKEN) -> tuple[int, str]:
    """Status of a CONNECT and, for a refusal, the proxy's reason."""
    credential = base64.b64encode(f"hermes:{token}".encode()).decode()
    reply = b""
    with socket.create_connection((EGRESS, 3128), timeout=15) as sock:
        sock.sendall(
            f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {credential}\r\n\r\n".encode()
        )
        while b"\r\n\r\n" not in reply:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
        if not reply.startswith(b"HTTP/1.1 "):
            return 0, ""
        status = int(reply.split(b" ")[1])
        if status == 200:
            return status, ""
        while chunk := sock.recv(4096):
            reply += chunk
    body = reply.partition(b"\r\n\r\n")[2]
    try:
        return status, str(json.loads(body)["error"])
    except (ValueError, KeyError):
        return status, ""


def https_status(host: str, path: str = "/") -> int:
    connection = http.client.HTTPSConnection(EGRESS, 3128, timeout=20, context=ssl.create_default_context())
    credential = base64.b64encode(f"hermes:{TOKEN}".encode()).decode()
    connection.set_tunnel(host, 443, headers={"Proxy-Authorization": f"Basic {credential}"})
    try:
        connection.request("GET", path, headers={"Host": host, "User-Agent": "hermes-egress-probe"})
        return connection.getresponse().status
    except OSError:
        return 0
    finally:
        connection.close()


def main(argv: list[str]) -> None:
    command = argv[0]
    if command == "network":
        emit(
            external_dns=resolves("example.com"),
            egress_dns=resolves(EGRESS),
            direct_public=direct("1.1.1.1", 443),
            direct_backend=direct(argv[1], 8000),
            direct_postgres=direct(argv[2], 5432),
        )
    elif command == "platform":
        allowed = raw(8080, completion_request())
        unstreamed = raw(8080, completion_request(stream=False))
        internal = raw(8080, completion_request("/v1/internal/hermes/sessions"))
        smuggled = raw(
            8080,
            completion_request()
            + b"POST /v1/internal/hermes/egress/authorize HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}",
        )
        emit(
            completion_status=int(allowed.split(b" ")[1]),
            completion_streamed=b"data: " in allowed,
            unstreamed_json=b'"object":"chat.completion"' in unstreamed.replace(b" ", b""),
            internal_status=int(internal.split(b" ")[1]),
            smuggled_responses=smuggled.count(b"HTTP/1.1 "),
            direct_backend_port=direct(EGRESS, 8000),
        )
    elif command == "connect":
        status, reason = connect_result(argv[1])
        emit(target=argv[1], status=status, reason=reason)
    elif command == "bad-token":
        status, reason = connect_result("pypi.org:443", "not-a-session-token")
        emit(status=status, reason=reason)
    elif command == "https":
        emit(host=argv[1], status=https_status(argv[1]))
    elif command == "npm":
        # What npm fetches first: a package's metadata from the registry.
        emit(status=https_status("registry.npmjs.org", "/left-pad"))
    elif command == "pip":
        proxy = f"http://hermes:{TOKEN}@{EGRESS}:3128"
        result = subprocess.run(
            [sys.executable, "-m", "pip", "download", "--no-deps", "--no-cache-dir", "-d", "/tmp/pip", "six==1.17.0"],
            env={**os.environ, "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        emit(pip_ok=result.returncode == 0, downloaded=sorted(os.listdir("/tmp/pip")) if result.returncode == 0 else [])
    elif command == "hold":
        credential = base64.b64encode(f"hermes:{TOKEN}".encode()).decode()
        sock = socket.create_connection((EGRESS, 3128), timeout=120)
        sock.sendall(
            f"CONNECT {argv[1]} HTTP/1.1\r\nHost: {argv[1]}\r\nProxy-Authorization: Basic {credential}\r\n\r\n".encode()
        )
        opened = sock.recv(4096).startswith(b"HTTP/1.1 200")
        print(json.dumps({"opened": opened}), flush=True)
        started = time.monotonic()
        try:
            closed = sock.recv(1) == b""
        except OSError:
            closed = True
        emit(closed=closed, after_seconds=round(time.monotonic() - started, 1))
    else:
        raise SystemExit(f"unknown probe {command}")


if __name__ == "__main__":
    main(sys.argv[1:])
