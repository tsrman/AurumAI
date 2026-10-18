#!/usr/bin/env python3
"""Validate semantic success in backend chat SSE captured by smoke tests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    pass


def parse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    event_type: str | None = None
    data_lines: list[str] = []

    def finish_event() -> None:
        nonlocal event_type, data_lines
        if event_type is None and not data_lines:
            return
        if event_type is None or not data_lines:
            raise ValidationError("SSE event is missing its event type or JSON data")
        try:
            data = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSON for SSE event {event_type}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError(f"SSE event {event_type} data is not an object")
        events.append((event_type, data))
        event_type = None
        data_lines = []

    for line in body.splitlines():
        if not line:
            finish_event()
        elif line.startswith("event: "):
            event_type = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))
    finish_event()
    if not events:
        raise ValidationError("SSE stream contains no events")
    return events


def _event_data(
    events: list[tuple[str, dict[str, Any]]], event_type: str
) -> list[dict[str, Any]]:
    return [data for kind, data in events if kind == event_type]


def _require_common_tool_events(events: list[tuple[str, dict[str, Any]]]) -> None:
    for event_type in ("tool-calls", "tool-result", "message-end", "done"):
        if not _event_data(events, event_type):
            raise ValidationError(f"missing required {event_type} event")


def validate_mcp_fetch(
    events: list[tuple[str, dict[str, Any]]], expected_title: str
) -> None:
    _require_common_tool_events(events)
    latest_tool_event = _event_data(events, "tool-result")[-1]
    outer_result = latest_tool_event.get("result")
    output = outer_result.get("output") if isinstance(outer_result, dict) else None
    tool_result = output.get("result") if isinstance(output, dict) else None
    if not isinstance(tool_result, dict) or tool_result.get("isError") is not False:
        raise ValidationError("latest MCP tool result is missing or reports an error")
    if expected_title not in json.dumps(tool_result, ensure_ascii=False):
        raise ValidationError("latest successful MCP tool result lacks expected title")

    expected_final = f"FETCH FIXTURE VERIFIED: {expected_title}"
    final_content = _event_data(events, "message-end")[-1].get("content")
    if final_content != expected_final:
        raise ValidationError(
            f"final assistant content must exactly equal {expected_final!r}"
        )


def validate_ordinary(
    events: list[tuple[str, dict[str, Any]]], expected_marker: str
) -> None:
    if _event_data(events, "error"):
        raise ValidationError("ordinary stream contains an error event")
    message_ends = _event_data(events, "message-end")
    if not message_ends:
        raise ValidationError("missing required message-end event")
    if not _event_data(events, "done"):
        raise ValidationError("missing required done event")
    final_content = message_ends[-1].get("content")
    if not isinstance(final_content, str) or expected_marker not in final_content:
        raise ValidationError("ordinary final assistant content lacks expected marker")


def validate_code(
    events: list[tuple[str, dict[str, Any]]],
    *,
    expected_final: str,
    minimum_results: int,
) -> None:
    _require_common_tool_events(events)
    run_commands = _event_data(events, "run-command")
    run_results = _event_data(events, "run-result")
    if len(run_commands) < minimum_results or len(run_results) < minimum_results:
        raise ValidationError(
            f"expected at least {minimum_results} run-command/run-result events"
        )
    for index, event in enumerate(run_results, start=1):
        result = event.get("result")
        if not isinstance(result, dict):
            raise ValidationError(f"run-result {index} has no result object")
        exit_code = result.get("exit_code")
        if type(exit_code) is not int or exit_code != 0:
            raise ValidationError(
                f"run-result {index} exit_code must be integer 0, got {exit_code!r}"
            )
        if result.get("timed_out") is not False:
            raise ValidationError(f"run-result {index} timed_out must be false")

    final_content = _event_data(events, "message-end")[-1].get("content")
    if final_content != expected_final:
        raise ValidationError(
            f"final assistant content must exactly equal {expected_final!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scenario", choices=("ordinary", "mcp-fetch", "code-greet", "code-multistep")
    )
    parser.add_argument("stream", type=Path)
    parser.add_argument("--expected-title")
    parser.add_argument("--expected-marker")
    args = parser.parse_args()

    try:
        events = parse_events(args.stream.read_text())
        if args.scenario == "ordinary":
            if not args.expected_marker:
                raise ValidationError("ordinary requires --expected-marker")
            validate_ordinary(events, args.expected_marker)
        elif args.scenario == "mcp-fetch":
            if not args.expected_title:
                raise ValidationError("mcp-fetch requires --expected-title")
            validate_mcp_fetch(events, args.expected_title)
        elif args.scenario == "code-greet":
            validate_code(events, expected_final="Hello, World!", minimum_results=1)
        else:
            validate_code(
                events, expected_final="FINAL RESULT: 142", minimum_results=2
            )
    except (OSError, ValidationError) as exc:
        print(f"SSE validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
