"""English-only source checks: Russian text lives only in the UI locale catalogs."""
from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import tokenize
from pathlib import Path

from .audit import Finding

CYRILLIC = re.compile(r"[\u0400-\u04ff]")
NODE_HELPER = Path(__file__).resolve().parents[1] / "check-release-language.mjs"
LOCALE_CATALOGS = frozenset({"business-ui/messages/ru.json", "admin-ui/messages/ru.json"})


def _python_lines(text: str, *, embedded: bool = True) -> set[int]:
    lines = {
        token.start[0] for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.COMMENT and CYRILLIC.search(token.string)
    }
    tree = ast.parse(text)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc and CYRILLIC.search(doc):
                lines.add(node.body[0].lineno)
        if embedded and isinstance(node, ast.Constant) and isinstance(node.value, str) and isinstance(parents.get(node), (ast.Assign, ast.AnnAssign)):
            value = node.value
            if "\n" in value and CYRILLIC.search(value) and re.search(r"(?:^|\n)(?:import |from |def |class |#!/)", value):
                try:
                    lines.update(node.lineno + line - 1 for line in _python_lines(value, embedded=False))
                except (SyntaxError, tokenize.TokenError):
                    lines.add(node.lineno)
    return lines


def _config_lines(text: str, name: str) -> set[int]:
    suffix = Path(name).suffix
    markers = ("--", "/*") if suffix == ".sql" else ("/*",) if suffix == ".css" else ("#",)
    lines: set[int] = set()
    quote: str | None = None
    index = 0
    while index < len(text):
        character = text[index]
        if quote:
            if character == "\\" and quote != "'":
                index += 2
                continue
            if character == quote:
                if suffix == ".sql" and index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character in "\"'":
            quote = character
            index += 1
            continue
        marker = next((m for m in markers if text.startswith(m, index)), None)
        if marker == "#" and index > 0 and not text[index - 1].isspace() and text[index - 1] not in ";":
            index += 1
            continue
        if marker:
            end = text.find("*/", index + 2) + 2 if marker == "/*" else text.find("\n", index)
            if end < index:
                end = len(text)
            if CYRILLIC.search(text[index:end]):
                lines.add(text.count("\n", 0, index) + 1)
            index = max(index + 1, end)
            continue
        index += 1
    return lines


def check_language(root: Path, files: tuple[str, ...]) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    javascript: list[dict[str, str]] = []
    cyrillic_lines: dict[str, set[int]] = {}
    for name in files:
        path = root / name
        if path.is_symlink() or not path.is_file():
            findings.append(Finding(name, None, "unsafe_source_file"))
            continue
        try:
            text = path.read_text()
        except UnicodeError:
            continue
        if not CYRILLIC.search(text) or name in LOCALE_CATALOGS:
            continue
        cyrillic_lines[name] = {i for i, line in enumerate(text.splitlines(), 1) if CYRILLIC.search(line)}
        suffix = path.suffix
        if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
            javascript.append({"path": name, "text": text})
            continue
        try:
            if suffix in {".md", ".rst", ".txt"}:
                lines = {i for i, line in enumerate(text.splitlines(), 1) if CYRILLIC.search(line)}
            elif suffix == ".py":
                lines = _python_lines(text)
            elif suffix in {".sh", ".sql", ".css", ".yml", ".yaml", ".toml", ".nft", ".conf"} or path.name.startswith("Dockerfile"):
                lines = _config_lines(text, name)
            else:
                lines = set()
            findings.extend(Finding(name, line, "non_english_prose") for line in sorted(lines))
        except (SyntaxError, tokenize.TokenError):
            findings.append(Finding(name, None, "language_parse_failed"))
    if javascript:
        process = subprocess.run(
            ["node", str(NODE_HELPER)], input=json.dumps(javascript), text=True,
            capture_output=True, timeout=60,
        )
        if process.returncode:
            findings.extend(Finding(item["path"], None, "typescript_checker_unavailable") for item in javascript)
        else:
            findings.extend(Finding(**item) for item in json.loads(process.stdout))
    # Anything Cyrillic that is not a comment is a runtime string; the comment
    # rule already covers its own lines, so each line is reported once.
    reported = {(f.path, f.line) for f in findings}
    for name, lines in cyrillic_lines.items():
        if (name, None) in reported:
            continue
        findings.extend(Finding(name, line, "non_english_string") for line in sorted(lines) if (name, line) not in reported)
    return tuple(sorted(findings, key=lambda f: (f.path, f.line or 0, f.rule)))
