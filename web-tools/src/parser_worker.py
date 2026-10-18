"""Seccomp-confined stdin/stdout worker for untrusted document parsing."""

import base64
import binascii
import json
import sys
from typing import Any

from src.extraction import ExtractionFailed, extract_html, extract_pdf
from src.parser_sandbox import ParserFailed, install_sandbox


def _parse_request(value: Any) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"content_type", "charset", "body"}:
        raise ExtractionFailed("unsupported_document")
    content_type = value["content_type"]
    charset = value["charset"]
    encoded = value["body"]
    if not isinstance(content_type, str) or not isinstance(encoded, str):
        raise ExtractionFailed("unsupported_document")
    if charset is not None and not isinstance(charset, str):
        raise ExtractionFailed("unsupported_document")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ExtractionFailed("unsupported_document") from exc
    if body.startswith(b"%PDF-"):
        result = extract_pdf(body)
    elif content_type in {"text/html", "application/xhtml+xml"}:
        result = extract_html(body, charset=charset)
    elif content_type == "application/pdf":
        raise ExtractionFailed("unsupported_document")
    else:
        raise ExtractionFailed("unsupported_document")
    return {
        "text": result.text,
        "title": result.title,
        "truncated": result.truncated,
        "page_count": result.page_count,
    }


def main() -> None:
    try:
        install_sandbox()
        request: Any = json.load(sys.stdin.buffer)
        response = _parse_request(request)
    except (ExtractionFailed, ParserFailed) as exc:
        response = {"error": exc.code}
    except Exception:
        response = {"error": "web_unavailable"}
    json.dump(response, sys.stdout, separators=(",", ":"), ensure_ascii=False)


if __name__ == "__main__":
    main()
