"""Document parsers: docling (PDF, DOCX, etc) with plain-text fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
import tempfile

logger = logging.getLogger(__name__)


@dataclass
class ParsedDocument:
    title: str
    text: str
    source_type: str
    metadata: dict[str, Any]


def _parse_plain_text(content: str, *, title: str = "document") -> ParsedDocument:
    return ParsedDocument(
        title=title,
        text=content,
        source_type="text",
        metadata={"parser": "plain"},
    )


def parse_with_docling(path: str | Path, fallback_source_type: str = "document") -> ParsedDocument:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))

    try:
        from docling.document_converter import DocumentConverter
        converter = DocumentConverter()
        result = converter.convert(str(path))
        markdown_text = result.document.export_to_markdown()
        
        return ParsedDocument(
            title=path.stem,
            text=markdown_text,
            source_type=fallback_source_type,
            metadata={"parser": "docling", "filename": path.name},
        )
    except ImportError:
        logger.warning("docling not installed, using fallback placeholder for %s", path)
        return ParsedDocument(
            title=path.stem,
            text=f"[Placeholder: install docling to extract content from {path.name}]",
            source_type=fallback_source_type,
            metadata={"parser": "fallback", "filename": path.name},
        )
    except Exception as exc:
        logger.warning("docling failed for %s: %s", path, exc)
        raise


def parse_upload(
    *,
    filename: str,
    content: bytes,
    content_type: Optional[str] = None,
) -> ParsedDocument:
    suffix = Path(filename).suffix.lower()
    
    # Use docling for supported formats
    if suffix in [".pdf", ".docx", ".doc", ".pptx", ".html", ".md"] or (content_type and ("pdf" in content_type or "word" in content_type.lower())):
        source_type = "pdf" if suffix == ".pdf" else ("docx" if "doc" in suffix else "document")
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".pdf") as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(content)
        
        try:
            parsed = parse_with_docling(tmp_path, fallback_source_type=source_type)
            parsed.title = Path(filename).stem
            parsed.metadata["filename"] = filename
            return parsed
        finally:
            tmp_path.unlink(missing_ok=True)

    text = content.decode("utf-8", errors="replace")
    return _parse_plain_text(text, title=Path(filename).stem)
