"""Validate local public documentation references inside a candidate boundary."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .audit import Finding


def _anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if not re.match(r"^#{1,6}\s", line):
            continue
        title = re.sub(r"^#{1,6}\s+", "", line).strip().lower()
        slug = re.sub(r"[^\w\s-]", "", title).replace(" ", "-")
        index = counts.get(slug, 0)
        counts[slug] = index + 1
        anchors.add(slug if index == 0 else f"{slug}-{index}")
    return anchors


def check_links(root: Path, files: tuple[str, ...]) -> tuple[Finding, ...]:
    root = root.resolve()
    selected = set(files)
    findings: list[Finding] = []
    for name in sorted(files):
        if not name.endswith(".md"):
            continue
        path = root / name
        fenced = False
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.match(r"\s*(?:```|~~~)", line):
                fenced = not fenced
                continue
            if fenced:
                continue
            for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", line):
                link = urlsplit(match.group(1).strip().strip("<>"))
                if link.scheme or link.netloc:
                    continue
                target = (path.parent / unquote(link.path)).resolve() if link.path else path.resolve()
                if root != target and root not in target.parents:
                    findings.append(Finding(name, number, "unsafe_document_link"))
                    continue
                relative = target.relative_to(root).as_posix()
                if relative not in selected or not target.is_file():
                    findings.append(Finding(name, number, "missing_document_link"))
                elif link.fragment and target.suffix == ".md" and unquote(link.fragment) not in _anchors(target.read_text()):
                    findings.append(Finding(name, number, "missing_document_anchor"))
    return tuple(findings)
