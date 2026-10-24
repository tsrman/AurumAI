"""Validate and format bounded JSON without silently discarding duplicate keys."""

import json
import math
from pathlib import Path
import sys
from typing import Any

MAX_BYTES = 1024 * 1024


def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key}")
        result[key] = value
    return result


class JsonNumber:
    def __init__(self, token: str) -> None:
        if not math.isfinite(float(token)):
            raise ValueError("nonfinite numbers are not valid JSON")
        self.token = token


def format_value(value: Any, depth: int = 0) -> str:
    """Keep validated decimal tokens intact instead of rounding through binary floats."""
    if isinstance(value, JsonNumber):
        return value.token
    indent = "  " * depth
    child_indent = "  " * (depth + 1)
    if isinstance(value, dict) and value:
        members = [
            child_indent + json.dumps(key, ensure_ascii=True) + ": "
            + format_value(value[key], depth + 1)
            for key in sorted(value)
        ]
        return "{\n" + ",\n".join(members) + "\n" + indent + "}"
    if isinstance(value, list) and value:
        members = [child_indent + format_value(item, depth + 1) for item in value]
        return "[\n" + ",\n".join(members) + "\n" + indent + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def reject_constant(value: str) -> Any:
    raise ValueError(f"nonfinite constant is not valid JSON: {value}")


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("usage: python3 scripts/format_json.py input.json")
        with Path(sys.argv[1]).open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("input exceeds 1 MiB")
        value = json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=object_pairs,
            parse_float=JsonNumber, parse_constant=reject_constant,
        )
        print(format_value(value))
        return 0
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
