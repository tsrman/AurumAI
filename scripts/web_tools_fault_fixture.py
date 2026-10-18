"""Bounded, UUID-scoped upstream faults; mounted only in acceptance fixtures."""

import json
import re
import threading
import time


class FaultCases:
    def __init__(self, *, barrier_seconds=45, deadline_seconds=20):
        self._cases = {}
        self._lock = threading.Lock()
        self._barrier_seconds = barrier_seconds
        self._deadline_seconds = deadline_seconds

    def response(self, target, method):
        match = re.fullmatch(
            r"/(fault|control)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
            r"(?:/(status|release|arm/(?:429|404|503|barrier|deadline)))?", target,
        )
        if match is None:
            return 404, "text/plain", b"unknown case"
        kind, identity, action = match.groups()
        with self._lock:
            case = self._cases.get(identity)
            if kind == "control" and method == "POST" and action and action.startswith("arm/"):
                if case is not None:
                    return 409, "text/plain", b"already armed"
                if len(self._cases) >= 64:
                    return 429, "text/plain", b"case limit"
                case = {"mode": action[4:], "attempts": 0, "finished": 0, "release": threading.Event()}
                self._cases[identity] = case
            elif case is None:
                return 404, "text/plain", b"unknown case"
            elif kind == "control" and action == "release" and method == "POST":
                case["release"].set()
            elif kind == "control" and not (action == "status" and method == "GET"):
                return 405, "text/plain", b"invalid action"
            if kind == "control":
                return 200, "application/json", json.dumps({
                    "attempts": case["attempts"], "released": case["release"].is_set(),
                    "finished": case["finished"],
                }).encode()
            if action is not None or method != "GET":
                return 405, "text/plain", b"invalid action"
            case["attempts"] += 1
            attempt = case["attempts"]
        mode = case["mode"]
        code = 200
        if mode in {"404", "429"}:
            code = int(mode)
        elif mode == "503":
            code = 503 if attempt == 1 else 200
        elif mode == "barrier":
            if not case["release"].wait(self._barrier_seconds):
                code = 504
        elif mode == "deadline":
            time.sleep(self._deadline_seconds)
            code = 503 if attempt == 1 else 200
        with self._lock:
            case["finished"] += 1
        body = ("<html><title>Owned upstream case</title><article>" +
                "Reserved domains are used for documentation and deterministic acceptance. " * 20 +
                "</article></html>").encode()
        return code, "text/html", body


def control_program(identity, action):
    """Fixed loopback control only, never accepts arbitrary URLs or code."""
    if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identity or "") is None:
        raise ValueError("invalid case identity")
    if action not in {"status", "release", "arm/429", "arm/404", "arm/503", "arm/barrier", "arm/deadline"}:
        raise ValueError("invalid case action")
    return "\n".join([
        "import json,urllib.request",
        f"request=urllib.request.Request('http://127.0.0.1/control/{identity}/{action}',method={'GET' if action == 'status' else 'POST'!r})",
        "data=json.load(urllib.request.urlopen(request,timeout=5))",
        "assert set(data)=={'attempts','released','finished'}",
        "assert type(data['released']) is bool and all(type(data[k]) is int and 0<=data[k]<=100 for k in ('attempts','finished'))",
        "print(json.dumps(data))",
    ])
