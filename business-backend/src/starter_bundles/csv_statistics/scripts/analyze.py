"""Compute bounded offline CSV statistics without third-party packages."""

import csv
import io
import json
import math
from pathlib import Path
import sys

MAX_BYTES = 1024 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 100


def analyze(path: str) -> dict[str, object]:
    with Path(path).open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("input exceeds 1 MiB")
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
    headers = next(reader, [])
    if not headers or any(not header.strip() for header in headers):
        raise ValueError("a nonempty header is required")
    if len(headers) > MAX_COLUMNS or len(set(headers)) != len(headers):
        raise ValueError("headers must be unique, with at most 100 columns")
    columns: list[list[str]] = [[] for _ in headers]
    rows = 0
    for row in reader:
        rows += 1
        if rows > MAX_ROWS:
            raise ValueError("input exceeds 10000 rows")
        if len(row) != len(headers):
            raise ValueError("each row must match the header width")
        for column, value in zip(columns, row, strict=True):
            column.append(value.strip())
    numeric: dict[str, object] = {}
    for header, column in zip(headers, columns, strict=True):
        values = []
        is_numeric = True
        for value in column:
            if not value:
                continue
            try:
                number = float(value)
            except ValueError:
                is_numeric = False
                continue
            if not math.isfinite(number):
                raise ValueError("nonfinite numbers are not supported")
            values.append(number)
        if is_numeric and values:
            total = math.fsum(values)
            numeric[header] = {
                "count": len(values), "missing": rows - len(values),
                "sum": total, "mean": total / len(values),
                "min": min(values), "max": max(values),
            }
    return {"rows": rows, "columns": headers, "numeric": numeric}


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("usage: python3 scripts/analyze.py input.csv")
        print(json.dumps(analyze(sys.argv[1]), allow_nan=False, sort_keys=True))
        return 0
    except (OSError, UnicodeError, ValueError, OverflowError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
